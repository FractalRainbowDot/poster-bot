"""Mandatory-signature forwarding: explicit consent, frozen review, legacy rescue."""
import asyncio
import json

import pytest
from aiogram.exceptions import TelegramNetworkError
from conftest import NOW
from test_live_composition import LiveApiSession, live_config, make_bot, OWNER
from test_ui_forward_intake import Intake, ORIGIN, PHOTO
from poster_bot.domain.content import Content, ForwardSource
from poster_bot.domain.time import Instant
from poster_bot.live import compose_live
from poster_bot.repo.drafts import DraftStore
from poster_bot.repo.plans import PlanStore
from poster_bot.ui import texts
from poster_bot.ui.actions import ActionStore
from poster_bot.ui.flow import OwnerFlow
from poster_bot.ui.state import State, UiStateRepo


class ReviewSession(LiveApiSession):
    """Real Bot API return shapes; validate limits and record exact wire payloads."""
    fail_content = False

    async def make_request(self, bot, method, timeout=None):
        name = method.__api_method__
        p = method.model_dump(mode="json", exclude_none=True,
                              fallback=lambda obj: str(obj))
        if name.startswith("send") or name == "forwardMessages":
            assert p["chat_id"] == OWNER, "no channel writes in these tests"
        assert name != "forwardMessages", "configured path must not preview unsigned"
        is_content = name in ("sendPhoto", "sendVideo", "sendMediaGroup") or (
            name == "sendMessage" and not p.get("reply_markup"))
        if is_content:
            if self.fail_content:
                raise TelegramNetworkError(method=method, message="offline failure")
            items = p["media"] if name == "sendMediaGroup" else [p]
            for item in items:
                body = item.get("text", item.get("caption", ""))
                assert len(body) <= (4096 if name == "sendMessage" else 1024)
                units = len(body.encode("utf-16-le")) // 2
                for e in item.get("entities", item.get("caption_entities", [])):
                    assert 0 <= e["offset"] < e["offset"] + e["length"] <= units
            if name != "sendMessage":
                self.requests.append((name, p))
                result = []
                for item in items:
                    raw = self._raw()
                    self.visible[raw["message_id"]] = dict(item)
                    result.append(raw)
                if name != "sendMediaGroup":
                    result = result[0]
                return self.check_response(bot=bot, method=method, status_code=200,
                    content=json.dumps({"ok": True, "result": result})).result
        return await super().make_request(bot, method, timeout)


def labels(h):
    return [b["text"] for row in h.card()["reply_markup"]["inline_keyboard"] for b in row]


def content_requests(requests):
    return [(m, p) for m, p in requests if m in (
        "sendPhoto", "sendVideo", "sendMediaGroup") or (
            m == "sendMessage" and not p.get("reply_markup"))]


CASES = [(k, n, s) for k, n, sizes in [
    ("text", 1, [12, 4096]), ("photo", 1, [12, 1024, 1025, 2048]),
    ("video", 1, [12, 1024, 1025, 2048]), ("photo", 2, [12, 1024, 2048]),
    ("video", 3, [12, 2048]), ("photo", 10, [12])] for s in sizes]


@pytest.mark.parametrize("kind,count,size", CASES)
def test_forward_continue_requires_explicit_mode_then_signed_copy_review(tmp_path, kind, count, size):
    async def scenario():
        session = ReviewSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            await h.message(text=texts.MENU_NEW_POST, message_id=2)
            for n in range(count):
                body = ("🔥" + ("word " * size)[:size - 1])
                if kind == "text":
                    body = (("word " * 60 + "\n\n") * size)[:size]
                payload = dict(message_id=40 + n, forward_origin=ORIGIN)
                entities = [{"type": "bold", "offset": 0, "length": 8}]
                if kind == "text":
                    payload.update(text=body, entities=entities)
                else:
                    media = {"photo": [dict(PHOTO[0], file_id=f"photo-{n}")]} if kind == "photo" else {
                        "video": {"file_id": f"video-{n}", "file_unique_id": f"uv{n}",
                                  "width": 800, "height": 600, "duration": 1}}
                    payload.update(**media, caption=body, caption_entities=entities)
                    if count > 1:
                        payload["media_group_id"] = "album"
                await h.message(**payload)
            raw = h.staged().to_dict()
            assert texts.BTN_FORWARD_AS_IS in labels(h)
            await h.press(texts.BTN_CONTINUE)
            assert h.card()["text"] == texts.FORWARD_PLANNED_ASK
            assert h.staged().to_dict() == raw
            assert not live.app.conn.execute("SELECT 1 FROM posts").fetchone()
            assert not content_requests(session.requests)
            assert all(not h.deleted(40 + n) for n in range(count))
            before = len(session.requests)
            await h.press(texts.BTN_COPY_SIGNED)
            first = content_requests(session.requests[before:])
            assert first and not any(m == "forwardMessages" for m, _ in first)
            before = len(session.requests)
            await h.press(texts.BTN_CONTINUE)
            pid = UiStateRepo(live.app.conn).get().payload["post_id"]
            stored = PlanStore(live.app.conn).get(pid)
            assert stored.logical.body == "\n\n".join([body] * count)
            assert not content_requests(session.requests[before:])
            slot = next(label for label in labels(h) if " → " in label)
            before = len(session.requests)
            await h.press(slot)
            assert UiStateRepo(live.app.conn).get().state is State.CONFIRM_SCHEDULE
            assert texts.BTN_CONFIRM in labels(h)
            assert not content_requests(session.requests[before:])
            assert not live.app.conn.execute("SELECT 1 FROM publications").fetchone()
            before = len(session.requests)
            await h.press(texts.BTN_CONFIRM)
            assert live.app.conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 1
            assert not content_requests(session.requests[before:])
            assert PlanStore(live.app.conn).get(pid) == stored
        finally:
            await live.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("target", ["draft", "scheduled", "paused", "stale"])
@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize("rescue", [False, True])
def test_replace_decision_keeps_existing_edit_binding(services, target, keep, rescue):
    """Real intake conflict (photo/audio), real callbacks and storage commit."""
    from planned_helpers import SIG
    from ui_flow_helpers import build_flow, tokens, present_reply
    from poster_bot.ui.intake import forward_media_candidate
    from poster_bot.domain.states import Status

    async def scenario():
        flow, transport = build_flow(services, signature=SIG)
        original = (Content(kind="forward", mode="forward", forward=ForwardSource(
            chat_id=OWNER, message_ids=(40,))) if rescue else
            Content(kind="text", text="original"))
        pid = services.drafts.create(original, now_epoch=NOW - 100)
        pub = (services.publications.schedule(pid, Instant(NOW + 3600), now_epoch=NOW)
               if target != "draft" else None)
        if target == "paused":
            services.publications.pause(pub.id, pub.version, now_epoch=NOW)
            pub = services.publications.get(pub.id)
        before = dict(services.conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())
        if rescue:
            if pub:
                reply = flow.pub_open(now=NOW, pub_id=pub.id)
                reply = await flow.press(now=NOW, token=tokens(reply)[texts.BTN_EDIT_TIME])
            else:
                reply = flow.draft_schedule_ask(now=NOW, post_id=pid)
            assert reply.text == texts.FORWARD_RESEND_ASK
            await flow.press(now=NOW, token=reply.rows[0][0].token)
            reply = await flow.forwarded(now=NOW, src_chat_id=OWNER, src_message_id=40,
                media_group_id=None, copy_candidate=forward_media_candidate(
                    "photo", "original-photo", "photo body", (), None))
            reply = await flow.press(now=NOW, token=tokens(reply)[texts.BTN_COPY_SIGNED])
            assert not reply.released_inputs  # the saved raw post still references 40
            assert flow.forward_source_referenced(chat_id=OWNER, message_id=40)
        else:
            if pub:
                reply = flow._publication_view(now=NOW, pub_id=pub.id)
                # Suspended rows remain editable without becoming sendable.
                reply = await flow.press(now=NOW, token=tokens(reply)[texts.BTN_EDIT_CONTENT])
            else:
                reply = flow.draft_edit_start(now=NOW, post_id=pid)
            await flow.press(now=NOW, token=tokens(reply)[texts.BTN_EDIT_ALL])
            await flow.media(now=NOW, kind="photo", file_id="original-photo",
                             caption="photo body")
        binding = {k: v for k, v in flow._ui().payload.items() if k != "staged"}
        reply = await flow.media(now=NOW, kind="audio", file_id="replacement-audio",
                                 caption="audio body")
        assert flow._ui().state is State.AWAIT_REPLACE
        reply = await flow.press(now=NOW, token=tokens(reply)[
            texts.BTN_KEEP if keep else texts.BTN_REPLACE])
        restored = flow._ui()
        # A complete candidate and its controls must be presented before any
        # editing path (including raw-forward recovery) offers confirmation.
        reply = await present_reply(flow, reply, now=NOW, owner_id=OWNER)
        if target == "stale":
            paused = services.publications.pause(pub.id, pub.version, now_epoch=NOW)
            current = services.publications.resume(pub.id, paused.version, now_epoch=NOW)
        # Every saved edit needs separate confirmation after copy consent.
        reply = await flow.press(now=NOW, token=tokens(reply)[texts.BTN_CONTINUE])
        assert dict(services.conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()) == before
        if target != "stale":
            await flow.press(now=NOW, token=tokens(reply)[texts.BTN_CONFIRM])
        assert restored.payload == dict(binding, staged=restored.arg("staged"))
        after = dict(services.conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())
        assert after["created_at"] == before["created_at"]
        stored = PlanStore(services.conn).get(pid)
        if target == "stale":
            assert after == before
            assert stored is None
            assert services.publications.get(pub.id) == current
            assert services.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
            assert services.conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 1
            assert services.conn.execute("SELECT COUNT(*) FROM post_plans").fetchone()[0] == 0
            assert not transport.calls
            return
        assert stored.logical.body == ("photo body" if keep else "audio body")
        assert stored.logical.media[0].file_id == ("original-photo" if keep else "replacement-audio")
        assert services.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
        assert services.conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == int(pub is not None)
        assert services.conn.execute("SELECT COUNT(*) FROM post_plans").fetchone()[0] == 1
        if pub:
            updated = services.publications.get(pub.id)
            assert updated.post_id == pid
            assert updated.scheduled_epoch == pub.scheduled_epoch
            assert updated.id == pub.id and updated.minute_epoch == pub.minute_epoch
            assert updated.status is Status.SUSPENDED
            assert updated.version == pub.version + 1
            assert not services.dispatch.due_publications(now_epoch=pub.scheduled_epoch)
        assert not transport.calls
    asyncio.run(scenario())


@pytest.mark.parametrize("editing", [False, True])
@pytest.mark.parametrize("keep", [False, True])
def test_replace_recovery_drops_old_decision_metadata(services, editing, keep):
    from planned_helpers import SIG
    from ui_flow_helpers import build_flow, tokens

    async def scenario():
        flow, _ = build_flow(services, signature=SIG)
        if editing:
            pid = services.drafts.create(Content(kind="text", text="old"), now_epoch=NOW)
            reply = flow.draft_edit_start(now=NOW, post_id=pid)
            await flow.press(now=NOW, token=tokens(reply)[texts.BTN_EDIT_ALL])
        else:
            await flow.new_post(now=NOW)
        await flow.media(now=NOW, kind="photo", file_id="photo", caption=None)
        reply = await flow.media(now=NOW, kind="audio", file_id="audio", caption=None)
        ui = flow._ui()
        # A durable pre-upgrade decision can carry obsolete confirmation and
        # provenance fields. Neither decision may resurrect that authority.
        payload = dict(ui.payload, pending_caption={"text": "obsolete"},
                       copy_consent={"approved": True}, provenance={"source": 40})
        flow._state.set(ui.state, payload, expected_version=ui.version, now_epoch=NOW)
        # Decisions are version-bound; rebuild controls for this durable snapshot.
        reply = flow._replay_reply(now=NOW)
        reply = await flow.press(now=NOW, token=tokens(reply)[
            texts.BTN_KEEP if keep else texts.BTN_REPLACE])
        restored = flow._ui()
        assert set(restored.payload) == ({"staged", "target", "post_id", "edit_mode",
                                         "edit_original", "edit_snapshot"} if editing else {"staged"})
        if editing:
            assert restored.arg("edit_mode") == "all"
        assert restored.state is (State.EDITING if editing else State.CREATING)
        await flow.press(now=NOW, token=tokens(reply)[texts.BTN_CONTINUE])
        assert services.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("keep", [False, True])
def test_replace_lost_ui_cas_preserves_durable_pending_input(services, monkeypatch, keep):
    from planned_helpers import SIG
    from ui_flow_helpers import build_flow, tokens
    from poster_bot.ui.state import StaleStateError

    async def scenario():
        flow, transport = build_flow(services, signature=SIG)
        original = Content(kind="text", text="saved")
        pid = services.drafts.create(original, now_epoch=NOW)
        reply = flow.draft_edit_start(now=NOW, post_id=pid)
        await flow.press(now=NOW, token=tokens(reply)[texts.BTN_EDIT_ALL])
        await flow.media(now=NOW, kind="photo", file_id="photo", caption="keep")
        reply = await flow.media(now=NOW, kind="audio", file_id="audio", caption="replace")
        pending = flow._ui()
        real_set = flow._state.set

        def lose_cas(state, payload, **kwargs):
            # Another transaction has retained this decision while advancing
            # its version. The attempted restore must not overwrite it.
            real_set(pending.state, pending.payload, expected_version=pending.version,
                     now_epoch=NOW)
            return real_set(state, payload, **kwargs)

        monkeypatch.setattr(flow._state, "set", lose_cas)
        with pytest.raises(StaleStateError):
            await flow.press(now=NOW, token=tokens(reply)[
                texts.BTN_KEEP if keep else texts.BTN_REPLACE])
        assert flow._ui().state is State.AWAIT_REPLACE
        assert flow._ui().payload == pending.payload
        assert services.drafts.get_content(pid) == original
        assert PlanStore(services.conn).get(pid) is None
        assert not transport.calls
    asyncio.run(scenario())


@pytest.mark.parametrize("after_schedule", [False, True])
def test_copy_preview_failure_never_falls_back_or_repeats_schedule(tmp_path, after_schedule):
    async def scenario():
        session = ReviewSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            await h.message(text=texts.MENU_NEW_POST, message_id=2)
            await h.message(text="source", message_id=40, forward_origin=ORIGIN)
            await h.press(texts.BTN_COPY_SIGNED)
            await h.press(texts.BTN_CONTINUE)
            pid = UiStateRepo(live.app.conn).get().payload["post_id"]
            frozen = PlanStore(live.app.conn).get(pid)
            slot = next(label for label in labels(h) if " → " in label)
            if after_schedule:
                await h.press(slot)
                await h.press(texts.BTN_CONFIRM)
            # Automatic review reuses an unchanged visible stack. Leave it
            # before injecting a send failure, so the next stored-card open
            # genuinely exercises failure and retry rather than a cache hit.
            await h.message(text=texts.MENU_PLAN if after_schedule else texts.MENU_DRAFTS,
                            message_id=42)
            row = next(label for label in labels(h) if "source" in label)
            session.fail_content = True
            await h.press(row)
            assert "Не удалось показать весь пост" in h.card()["text"]
            assert texts.BTN_CONFIRM not in labels(h)
            assert live.app.conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == int(after_schedule)
            assert PlanStore(live.app.conn).get(pid) == frozen
            assert not any(m == "forwardMessages" for m, _ in session.requests)
            session.fail_content = False
            await h.press("Повторить предпросмотр")
            assert live.app.conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == int(after_schedule)
            assert PlanStore(live.app.conn).get(pid) == frozen
            assert "Повторить предпросмотр" not in labels(h)
        finally:
            await live.close()
    asyncio.run(scenario())


def test_legacy_as_is_capability_prompts_without_persisting_or_deleting(tmp_path):
    async def scenario():
        session = ReviewSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            await h.message(text=texts.MENU_NEW_POST, message_id=2)
            await h.message(text="source", message_id=40, forward_origin=ORIGIN)
            raw = h.staged().to_dict()
            # Simulate a still-valid server capability created by the old UI.
            token = ActionStore(live.app.conn).create("forward_as_is", now_epoch=NOW)
            h.card()["reply_markup"]["inline_keyboard"].append([
                {"text": "old", "callback_data": token}])
            await h.press("old")
            assert h.card()["text"] == texts.FORWARD_PLANNED_ASK
            assert h.staged().to_dict() == raw
            assert not h.deleted(40)
            assert not live.app.conn.execute("SELECT 1 FROM posts").fetchone()
            assert not content_requests(session.requests)
            await h.press(texts.BTN_CANCEL)
            assert not h.deleted(40)
        finally:
            await live.close()
    asyncio.run(scenario())


def test_copy_capability_is_bound_to_current_snapshot_and_single_use(tmp_path):
    async def scenario():
        session = ReviewSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            await h.message(text=texts.MENU_NEW_POST, message_id=2)
            await h.message(photo=PHOTO, message_id=40, forward_origin=ORIGIN,
                            media_group_id="album")
            old = next(b.copy() for row in h.card()["reply_markup"]["inline_keyboard"]
                       for b in row if b["text"] == texts.BTN_COPY_SIGNED)
            await h.message(photo=PHOTO, message_id=41, forward_origin=ORIGIN,
                            media_group_id="album")
            before = UiStateRepo(live.app.conn).get()
            h.card()["reply_markup"]["inline_keyboard"].append([
                dict(old, text="old-copy")])
            await h.press("old-copy")
            assert UiStateRepo(live.app.conn).get() == before
            assert not h.deleted(40) and not h.deleted(41)
            accepted = next(b.copy() for row in h.card()["reply_markup"]["inline_keyboard"]
                            for b in row if b["text"] == texts.BTN_COPY_SIGNED)
            await h.press(texts.BTN_COPY_SIGNED)
            copied = UiStateRepo(live.app.conn).get()
            h.card()["reply_markup"]["inline_keyboard"].append([
                dict(accepted, text="replay-copy")])
            await h.press("replay-copy")
            assert UiStateRepo(live.app.conn).get() == copied
            assert not live.app.conn.execute("SELECT 1 FROM posts").fetchone()
        finally:
            await live.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("scheduled", [False, True])
def test_saved_raw_forward_requires_resend_and_replaces_same_post_only_on_completion(tmp_path, scheduled):
    async def scenario():
        session = ReviewSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            content = Content(kind="forward", mode="forward",
                              forward=ForwardSource(chat_id=OWNER, message_ids=(40,)))
            pid = DraftStore(live.app.conn).create(content, now_epoch=NOW - 100)
            app = live.app
            flow = OwnerFlow(conn=app.conn, settings=app.settings, drafts=app.drafts,
                publications=app.publications, dispatch=app.dispatch,
                manual_times=app.manual_times, journal=app.journal,
                preview=live.transport, channel_id=-100222,
                signature=live_config(tmp_path).signature, plans=PlanStore(app.conn))
            pub = flow._publications.schedule(pid, Instant(NOW + 3600), now_epoch=NOW) if scheduled else None
            before = dict(live.app.conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())
            if pub:
                reply = flow.pub_open(now=NOW, pub_id=pub.id)
                from ui_flow_helpers import tokens
                reply = await flow.press(now=NOW, token=tokens(reply)[texts.BTN_EDIT_TIME])
            else:
                reply = flow.draft_schedule_ask(now=NOW, post_id=pid)
            assert reply.text == texts.FORWARD_RESEND_ASK
            assert not content_requests(session.requests)
            assert dict(live.app.conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()) == before
            if pub:
                assert flow._publications.get(pub.id) == pub
                for action in (flow.A_PUB_EDIT_TIME, flow.A_PUB_RESUME, flow.A_PUB_REPLAN):
                    token = ActionStore(app.conn).create(action, now_epoch=NOW,
                        publication_id=pub.id, expected_version=pub.version)
                    blocked = await flow.press(now=NOW, token=token)
                    assert blocked.text == texts.FORWARD_RESEND_ASK
                    assert flow._publications.get(pub.id) == pub
                    reply = blocked
            else:
                blocked = flow._confirm_schedule_view(now=NOW, post_id=pid,
                                                       instant=Instant(NOW + 3600))
                assert blocked.text == texts.FORWARD_RESEND_ASK
                assert blocked.review_post_id is None
                assert not app.conn.execute("SELECT 1 FROM publications").fetchone()
                reply = blocked
            # Execute the explicit resend capability, then supply a fresh forward.
            await flow.press(now=NOW, token=reply.rows[0][0].token)
            await h.message(text="replacement", message_id=41, forward_origin=ORIGIN)
            await h.press(texts.BTN_COPY_SIGNED)
            assert DraftStore(live.app.conn).get_content(pid) == content
            await h.press(texts.BTN_CONTINUE)
            assert dict(live.app.conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()) == before
            if pub:
                assert flow._publications.get(pub.id) == pub
            await h.press(texts.BTN_CONFIRM)
            after = dict(live.app.conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())
            assert after["created_at"] == before["created_at"]
            assert PlanStore(live.app.conn).get(pid).logical.body == "replacement"
            assert live.app.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
            assert not h.deleted(40)
            if pub:
                updated = flow._publications.get(pub.id)
                assert updated.scheduled_epoch == pub.scheduled_epoch
                assert updated.status.value == "suspended"
        finally:
            await live.close()
    asyncio.run(scenario())
