"""Forward intake regressions through the configured, offline live Dispatcher."""
from __future__ import annotations

import asyncio

import pytest
from aiogram.types import Update

from conftest import NOW
from test_live_composition import LiveApiSession, live_config, make_bot, OWNER
from test_ui_handlers_offline import _raw_message
from poster_bot.live import compose_live
from poster_bot.ui import texts
from poster_bot.ui.staged import Staged
from poster_bot.ui.state import UiStateRepo


ORIGIN = {"type": "channel", "date": NOW,
          "chat": {"id": -100555, "type": "channel", "title": "Source"},
          "message_id": 77}
PHOTO = [{"file_id": "photo-file", "file_unique_id": "unique",
          "width": 800, "height": 600}]


@pytest.mark.parametrize("saved", ["legacy_forward", "unreadable"])
def test_explicit_copy_preserves_sources_needed_by_saved_posts(tmp_path, saved):
    async def scenario():
        from poster_bot.domain.content import Content, ForwardSource
        from poster_bot.repo.drafts import DraftStore
        from poster_bot.repo.plans import PlanStore
        session = LiveApiSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            drafts = DraftStore(live.app.conn)
            original = Content(kind="forward", mode="forward",
                               forward=ForwardSource(chat_id=OWNER, message_ids=(40,)))
            pid = drafts.create(original, now_epoch=NOW)
            if saved == "unreadable":
                live.app.conn.execute("UPDATE posts SET content_json='{}' WHERE id=?", (pid,))
                live.app.conn.commit()
            before = dict(live.app.conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())
            assert not live.app.conn.execute("SELECT 1 FROM ui_effects WHERE effect_key=?",
                                             (f"{OWNER}:40",)).fetchone()
            await h.message(text=texts.MENU_NEW_POST, message_id=2)
            await h.message(message_id=40, forward_origin=ORIGIN, photo=PHOTO,
                            caption="a" * 1025)
            label = next(b["text"] for row in h.card()["reply_markup"]["inline_keyboard"]
                         for b in row if "копию" in b["text"])
            await h.press(label)
            assert h.staged().kind != "forward"
            assert not h.deleted(40)
            assert "сохран" in h.card()["text"].lower()
            await h.press(texts.BTN_CONTINUE)
            assert dict(live.app.conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()) == before
            new_pid = next(p for p in drafts.list_drafts() if p != pid)
            stored = PlanStore(live.app.conn).get(new_pid)
            assert stored.logical.body == "a" * 1025
            assert len(stored.plan.parts) == 2
            assert not h.deleted(40)
            assert not session.sent_to_channel()
        finally:
            await live.close()
    asyncio.run(scenario())


class Intake:
    def __init__(self, live, session):
        self.live, self.session, self.seq = live, session, 0

    async def message(self, **fields):
        self.seq += 1
        raw = _raw_message(**fields)
        self.session.visible[raw["message_id"]] = raw
        await self.live.dispatcher.feed_update(self.live.bot, Update.model_validate(
            {"update_id": self.seq, "message": raw}))

    def card(self):
        return next(p for m, p in reversed(self.session.requests)
                    if m in ("sendMessage", "editMessageText")
                    and p.get("reply_markup", {}).get("inline_keyboard"))

    async def press(self, label):
        card = self.card()
        token = next(b["callback_data"] for row in
                     card["reply_markup"]["inline_keyboard"] for b in row
                     if b["text"] == label)
        self.seq += 1
        from poster_bot.ui.navigation import NavigationStore
        card_id = NavigationStore(self.live.app.conn, OWNER).get(OWNER)
        await self.live.dispatcher.feed_update(self.live.bot, Update.model_validate({
            "update_id": self.seq, "callback_query": {
                "id": f"cb-{self.seq}", "from": {"id": OWNER, "is_bot": False,
                "first_name": "Owner"}, "chat_instance": "ci", "data": token,
                "message": _raw_message(message_id=card_id, text="card")}}))

    def staged(self):
        return Staged.from_dict(UiStateRepo(self.live.app.conn).get().payload["staged"])

    def deleted(self, mid):
        return any(m == "deleteMessage" and p["chat_id"] == OWNER
                   and p["message_id"] == mid for m, p in self.session.requests)


@pytest.mark.parametrize("size", [1025, 2048])
@pytest.mark.parametrize("media_kind", ["photo", "video"])
def test_long_forward_caption_preserved_until_copy_consent(tmp_path, size, media_kind):
    async def scenario():
        session = LiveApiSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        caption = "🔥" + "a" * (size - 1)
        entities = [{"type": "bold", "offset": 0, "length": size + 1}]
        try:
            await h.message(text=texts.MENU_NEW_POST, message_id=2)
            media = {"photo": PHOTO} if media_kind == "photo" else {"video": {
                "file_id": "video-file", "file_unique_id": "uv", "width": 800,
                "height": 600, "duration": 1}}
            await h.message(message_id=40, forward_origin=ORIGIN, **media,
                            caption=caption, caption_entities=entities)
            assert not any(texts.OVER_LIMITS_PREFIX in p.get("text", "")
                           for m, p in session.requests)
            assert not h.deleted(40)
            staged = h.staged()
            assert staged.forward_ids == [40]
            assert caption in str(staged.to_dict())
            label = next(b["text"] for row in h.card()["reply_markup"]["inline_keyboard"]
                         for b in row if "копию" in b["text"])
            receipts = []
            def before_delete(mid):
                if mid == 40:
                    receipts.append((h.staged().text, h.staged().entities,
                                     live.app.conn.in_transaction))
            session.before_delete = before_delete
            # Copy consent now renders the durable candidate immediately;
            # Continue reuses that stack rather than sending it again.
            before_preview = len(session.requests)
            await h.press(label)
            assert h.staged().text == caption
            assert h.staged().entities[0].length == size + 1
            assert h.deleted(40)
            assert receipts == [(caption, h.staged().entities, False)]
            await h.press(texts.BTN_CONTINUE)
            from poster_bot.repo.plans import PlanStore
            row = live.app.conn.execute("SELECT id FROM posts").fetchone()
            stored = PlanStore(live.app.conn).get(row["id"])
            assert stored.logical.body == caption
            assert stored.logical.entities[0].length == size + 1
            assert "".join(p.body_slice for p in stored.plan.parts) == caption
            assert len(stored.plan.parts) == 2
            preview_requests = session.requests[before_preview:]
            for part in stored.plan.parts:
                content = part.content
                if content.kind == "text":
                    assert len(content.text) <= 4096
                    emitted = next(p for m, p in preview_requests
                                   if m == "sendMessage" and p["text"] == content.text)
                    assert emitted["chat_id"] == OWNER
                    assert emitted["entities"][0]["type"] == "bold"
                    assert emitted["entities"][0]["length"] == size + 1
                else:
                    assert len(content.media.caption or "") <= 1024
                    method = "sendPhoto" if media_kind == "photo" else "sendVideo"
                    emitted = next(p for m, p in preview_requests if m == method)
                    assert emitted["caption"] == content.media.caption
                    assert emitted[media_kind] == content.media.file_id
                    assert emitted["chat_id"] == OWNER
            assert not session.sent_to_channel()
        finally:
            await live.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [
    {"photo": PHOTO, "caption": "🔥", "caption_entities": [
        {"type": "bold", "offset": 1, "length": 1}]},
    {"document": {"file_id": "doc", "file_unique_id": "ud", "file_name": "file.pdf"}},
])
def test_terminal_forward_discarded_not_captured(tmp_path, payload):
    async def scenario():
        session = LiveApiSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            await h.message(text=texts.MENU_NEW_POST, message_id=2)
            await h.message(message_id=40, forward_origin=ORIGIN, **payload)
            assert h.deleted(40)
            conn = live.app.conn
            assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
            assert conn.execute("SELECT 1 FROM ui_effects WHERE effect_key=?",
                                (f"discarded:{OWNER}:40",)).fetchone()
            assert not conn.execute("SELECT 1 FROM ui_effects WHERE effect_key=?",
                                    (f"{OWNER}:40",)).fetchone()
            await h.message(message_id=40, forward_origin=ORIGIN, **payload)
            assert conn.execute("SELECT COUNT(*) FROM ui_effects WHERE effect_key=?",
                                (f"discarded:{OWNER}:40",)).fetchone()[0] == 1
            assert all(p["chat_id"] == OWNER for m, p in session.requests
                       if m == "deleteMessage")
        finally:
            await live.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("screen", ["settings", "settings_input", "draft", "publication", "manual_time"])
@pytest.mark.parametrize("payload", [{"text": texts.MENU_RESTART}, {"photo": PHOTO, "caption": "valid"}])
def test_valid_wrong_stage_forward_preserves_screen_then_discards(tmp_path, screen, payload):
    async def scenario():
        from conftest import text_content
        from poster_bot.domain.time import Instant
        from poster_bot.repo.drafts import DraftStore
        from poster_bot.repo.publications import PublicationsService
        from poster_bot.journal import Journal
        from poster_bot.ui.state import State
        session = LiveApiSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            conn = live.app.conn
            pid = DraftStore(conn).create(text_content("saved post"), now_epoch=NOW)
            data = {"post_id": pid}
            if screen == "publication":
                pub = PublicationsService(conn, Journal(conn)).schedule(pid, Instant(NOW + 3600), now_epoch=NOW)
                data = {"pub_id": pub.id}
            elif screen == "settings_input":
                data = {"key": "timezone"}
            elif screen == "manual_time":
                data.update(purpose="schedule", time_form={"date": "2030-01-01",
                            "time": "08:10", "field": "time"})
            elif screen == "settings":
                data = {}
            ui = UiStateRepo(conn)
            ui.set(State(screen), data, expected_version=ui.get().version, now_epoch=NOW)
            before = ui.get()
            posts = [dict(row) for row in conn.execute("SELECT * FROM posts")]
            receipts = []
            session.before_delete = lambda mid: receipts.append((mid, ui.get(), bool(conn.execute(
                "SELECT 1 FROM ui_effects WHERE effect_key=?", (f"discarded:{OWNER}:40",)).fetchone())))
            await h.message(message_id=40, forward_origin=ORIGIN, **payload)
            assert ui.get() == before
            assert h.deleted(40)
            assert receipts == [(40, before, True)]
            assert "пересланное" in h.card()["text"]
            assert not conn.execute("SELECT 1 FROM ui_effects WHERE effect_key=?", (f"{OWNER}:40",)).fetchone()
            assert [dict(row) for row in conn.execute("SELECT * FROM posts")] == posts
            assert not session.sent_to_channel()
        finally:
            await live.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("blocked", ["guidance", "receipt", "saved_source", "staged_source", "unreadable"])
def test_wrong_stage_forward_retained_when_cleanup_not_proven(tmp_path, monkeypatch, blocked):
    async def scenario():
        from poster_bot.domain.content import Content, ForwardSource
        from poster_bot.repo.drafts import DraftStore
        from poster_bot.ui.state import State
        session = LiveApiSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            conn = live.app.conn
            ui = UiStateRepo(conn)
            data = {}
            if blocked in ("saved_source", "unreadable"):
                pid = DraftStore(conn).create(Content(kind="forward", mode="forward",
                    forward=ForwardSource(chat_id=OWNER, message_ids=(40,))), now_epoch=NOW)
                if blocked == "unreadable":
                    conn.execute("UPDATE posts SET content_json='{}' WHERE id=?", (pid,))
                    conn.commit()
            elif blocked == "staged_source":
                data = {"pending_replace": {"kind": "forward", "forward_chat_id": OWNER,
                                             "forward_ids": [40]}}
            ui.set(State.SETTINGS_INPUT, data, expected_version=ui.get().version, now_epoch=NOW)
            before = ui.get()
            if blocked == "guidance":
                original = session.make_request
                async def fail(bot, method, timeout=None):
                    if method.__api_method__ in ("editMessageText", "sendMessage"):
                        from aiogram.exceptions import TelegramBadRequest
                        raise TelegramBadRequest(method=method, message="offline failure")
                    return await original(bot, method, timeout)
                monkeypatch.setattr(session, "make_request", fail)
            elif blocked == "receipt":
                def fail(*args, **kwargs):
                    raise RuntimeError("synthetic durable receipt failure")
                monkeypatch.setattr(UiStateRepo, "record_effect", fail)
            fields = dict(message_id=40, forward_origin=ORIGIN, photo=PHOTO, caption="valid")
            if blocked in ("guidance", "receipt"):
                with pytest.raises(Exception):
                    await h.message(**fields)
            else:
                await h.message(**fields)
            assert ui.get() == before
            assert not h.deleted(40)
            assert not conn.execute("SELECT 1 FROM ui_effects WHERE effect_key IN (?, ?)",
                (f"{OWNER}:40", f"discarded:{OWNER}:40")).fetchone()
            assert not session.sent_to_channel()
        finally:
            await live.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("origin", [ORIGIN,
    {"type": "hidden_user", "date": NOW, "sender_user_name": "Source"},
    {"type": "user", "date": NOW, "sender_user": {"id": 555, "is_bot": False, "first_name": "Source"}},
    {"type": "chat", "date": NOW, "sender_chat": {"id": -555, "type": "supergroup", "title": "Source"}},
])
@pytest.mark.parametrize("payload", [
    {"text": "🔥", "entities": [{"type": "bold", "offset": 1, "length": 1}]},
    {"photo": PHOTO, "caption": "🔥", "caption_entities": [{"type": "bold", "offset": 1, "length": 1}]},
])
def test_malformed_forward_saved_source_protected_for_every_origin(tmp_path, origin, payload):
    async def scenario():
        from poster_bot.domain.content import Content, ForwardSource
        from poster_bot.repo.drafts import DraftStore
        session = LiveApiSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            conn = live.app.conn
            original = Content(kind="forward", mode="forward",
                               forward=ForwardSource(chat_id=OWNER, message_ids=(40,)))
            drafts = DraftStore(conn)
            pid = drafts.create(original, now_epoch=NOW)
            await h.message(text=texts.MENU_SETTINGS, message_id=2)
            before = UiStateRepo(conn).get()
            await h.message(message_id=40, forward_origin=origin, **payload)
            assert UiStateRepo(conn).get() == before
            assert drafts.get_content(pid) == original
            assert not h.deleted(40)
            assert not conn.execute("SELECT 1 FROM ui_effects WHERE effect_key IN (?, ?)",
                (f"{OWNER}:40", f"discarded:{OWNER}:40")).fetchone()
            assert not session.sent_to_channel()
        finally:
            await live.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("blocked", ["guidance", "receipt", "saved_source", "staged_source"])
def test_invalid_forward_retained_when_cleanup_not_proven(tmp_path, monkeypatch, blocked):
    async def scenario():
        session = LiveApiSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            await h.message(text=texts.MENU_NEW_POST, message_id=2)
            if blocked == "saved_source":
                from poster_bot.domain.content import Content, ForwardSource
                from poster_bot.repo.drafts import DraftStore
                DraftStore(live.app.conn).create(Content(kind="forward", mode="forward",
                    forward=ForwardSource(chat_id=OWNER, message_ids=(40,))), now_epoch=NOW)
            elif blocked == "staged_source":
                await h.message(message_id=40, forward_origin=ORIGIN, photo=PHOTO,
                                caption="valid")
            elif blocked == "guidance":
                # Both edit and fallback sends fail: no guidance was delivered.
                original = session.make_request
                async def fail(bot, method, timeout=None):
                    if method.__api_method__ in ("editMessageText", "sendMessage"):
                        from aiogram.exceptions import TelegramBadRequest
                        raise TelegramBadRequest(method=method, message="offline failure")
                    return await original(bot, method, timeout)
                monkeypatch.setattr(session, "make_request", fail)
            elif blocked == "receipt":
                def fail(*args, **kwargs):
                    raise RuntimeError("synthetic durable receipt failure")
                monkeypatch.setattr(UiStateRepo, "record_effect", fail)
            fields = dict(message_id=40, forward_origin=ORIGIN, photo=PHOTO,
                          caption="🔥", caption_entities=[
                              {"type": "bold", "offset": 1, "length": 1}])
            if blocked in ("guidance", "receipt"):
                with pytest.raises(Exception):
                    await h.message(**fields)
            else:
                await h.message(**fields)
            assert not h.deleted(40)
            assert not live.app.conn.execute(
                "SELECT 1 FROM ui_effects WHERE effect_key=?",
                (f"discarded:{OWNER}:40",)).fetchone()
        finally:
            await live.close()
    asyncio.run(scenario())


def test_long_forward_group_replay_and_later_text_preserve_order(tmp_path):
    async def scenario():
        session = LiveApiSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            await h.message(text=texts.MENU_NEW_POST, message_id=2)
            first = dict(message_id=40, forward_origin=ORIGIN, photo=PHOTO,
                         caption="a" * 1025, media_group_id="group")
            await h.message(**first)
            await h.message(**first)  # replay must not add another item/body
            await h.message(message_id=41, forward_origin=ORIGIN, photo=PHOTO,
                            caption="🔥 end", media_group_id="group",
                            caption_entities=[{"type": "bold", "offset": 0, "length": 2}])
            assert h.staged().forward_ids == [40, 41]
            label = next(b["text"] for row in h.card()["reply_markup"]["inline_keyboard"]
                         for b in row if "копию" in b["text"])
            await h.press(label)
            expected = "a" * 1025 + "\n\n🔥 end"
            assert h.staged().text == expected
            assert len(h.staged().album_items) == 2
            assert h.staged().entities[0].offset == 1027
            assert h.deleted(40) and h.deleted(41)
            await h.message(message_id=42, text="later")
            assert h.staged().text == expected + "\n\nlater"
        finally:
            await live.close()
    asyncio.run(scenario())


def test_unsplittable_forward_keeps_raw_snapshot_and_source(tmp_path):
    async def scenario():
        session = LiveApiSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            await h.message(text=texts.MENU_NEW_POST, message_id=2)
            caption = "a" * 4097
            await h.message(message_id=40, forward_origin=ORIGIN, photo=PHOTO,
                            caption=caption)
            label = next(b["text"] for row in h.card()["reply_markup"]["inline_keyboard"]
                         for b in row if "копию" in b["text"])
            await h.press(label)
            assert not h.deleted(40)
            assert h.staged().kind == "forward"
            assert caption in str(h.staged().to_dict())
            assert "разб" in h.card()["text"]
            assert live.app.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
        finally:
            await live.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("audio_first", [True, False])
def test_raw_group_with_unsupported_item_cannot_release_partial_copy(tmp_path, audio_first):
    async def scenario():
        session = LiveApiSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        audio = {"file_id": "audio-file", "file_unique_id": "u", "duration": 1}
        chunks = [dict(audio=audio), dict(photo=PHOTO, caption="a" * 1025)]
        if not audio_first:
            chunks.reverse()
        try:
            await h.message(text=texts.MENU_NEW_POST, message_id=2)
            for mid, chunk in zip((40, 41), chunks):
                await h.message(message_id=mid, forward_origin=ORIGIN,
                                media_group_id="group", **chunk)
            assert h.staged().forward_ids == [40, 41]
            before = UiStateRepo(live.app.conn).get()
            label = next(b["text"] for row in h.card()["reply_markup"]["inline_keyboard"]
                         for b in row if "копию" in b["text"])
            await h.press(label)
            assert UiStateRepo(live.app.conn).get() == before
            assert "Невозможно" in h.card()["text"]
            assert not h.deleted(40) and not h.deleted(41)
        finally:
            await live.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("identity", [
    {"from": {"id": 999, "is_bot": False, "first_name": "Other"}},
    {"chat": {"id": -100999, "type": "channel"}},
])
def test_invalid_forward_unauthorized_has_no_effects(tmp_path, identity):
    async def scenario():
        session = LiveApiSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            before = len(session.requests)
            await h.message(message_id=40, forward_origin=ORIGIN, photo=PHOTO,
                            caption="🔥", caption_entities=[
                                {"type": "bold", "offset": 1, "length": 1}], **identity)
            assert len(session.requests) == before
            assert live.app.conn.execute("SELECT COUNT(*) FROM ui_effects").fetchone()[0] == 0
            assert live.app.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
        finally:
            await live.close()
    asyncio.run(scenario())
