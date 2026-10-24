"""Real Dispatcher/fake Telegram chronology and safe private input cleanup."""
import pytest

from conftest import NOW, text_content
from test_ui_handlers_offline import OWNER, _Harness
from poster_bot.ui import texts
from poster_bot.ui.state import State


def deleted(h):
    return [p["message_id"] for m, p in h.session.requests if m == "deleteMessage"]


@pytest.mark.parametrize("kind", ["photo", "video", "audio", "album"])
def test_media_sources_removed_only_after_committed_file_ids(services, kind):
    h = _Harness(services)
    h.message(text="/start")
    h.press_text(texts.MENU_NEW_POST)
    receipts = []

    def before_delete(mid):
        if mid in (40, 41):
            receipts.append((mid, h.ui.has_effect(f"{OWNER}:{mid}"),
                             str(h.ui.get().payload), services.conn.in_transaction))

    h.session.before_delete = before_delete
    media = {"file_id": "reusable-file", "file_unique_id": "unique"}
    if kind in ("photo", "album"):
        media.update(width=100, height=100)
        event = {"photo": [media]}
    else:
        media.update(duration=3)
        if kind == "video":
            media.update(width=100, height=100)
        event = {kind: media}
    if kind == "album":
        event["media_group_id"] = "album-1"
    h.message(message_id=40, **event)
    if kind == "album":
        event["photo"][0]["file_id"] = "second-file"
        h.message(message_id=41, **event)
    assert len(receipts) == (2 if kind == "album" else 1)
    assert all(captured and "reusable-file" in payload and not transaction
               for _, captured, payload, transaction in receipts)
    assert 40 not in h.session.visible
    assert list(h.session.visible)[-1] == h.card_id()
    h.press_text(texts.BTN_CONTINUE)
    saved = services.drafts.get_content(services.drafts.list_drafts()[0])
    assert "reusable-file" in str(saved)


def test_unrelated_control_input_removed_but_rejected_content_retained(services):
    h = _Harness(services)
    h.message(text="/start")
    h.message(text="not a command", message_id=30)
    assert 30 not in h.session.visible
    assert h.flow.input_discarded(f'{OWNER}:30')
    assert not h.flow.input_captured(f'{OWNER}:30')
    h.press_text(texts.MENU_NEW_POST)
    for mid, event in [
        (40, {"voice": {"file_id": "v", "file_unique_id": "u", "duration": 3}}),
        (41, {"photo": [{"file_id": "p", "file_unique_id": "u",
                         "width": 100, "height": 100}], "caption": "x" * 1025}),
        (42, {"audio": {"file_id": "a", "file_unique_id": "u", "duration": 3},
              "media_group_id": "album"}),
    ]:
        old = h.card_id()
        h.message(message_id=mid, **event)
        assert mid in h.session.visible
        assert not h.ui.has_effect(f"{OWNER}:{mid}")
        assert old not in h.session.visible
        assert list(h.session.visible)[-1] == h.card_id()
        assert h.last_text()


def test_failed_durable_intake_neither_deletes_input_nor_replaces_card(services, monkeypatch):
    h = _Harness(services)
    h.message(text="/start")
    h.press_text(texts.MENU_NEW_POST)
    old, requests = h.card_id(), list(h.session.requests)

    def unavailable(*args, **kwargs):
        raise RuntimeError("isolated persistence failure")

    monkeypatch.setattr(h.flow._state, "set", unavailable)
    with pytest.raises(RuntimeError, match="persistence failure"):
        h.message(text="not stored", message_id=40)
    assert h.session.requests == requests
    assert h.card_id() == old
    assert 40 in h.session.visible


def test_navigation_write_failure_preserves_old_working_card(services, monkeypatch):
    from poster_bot.ui.navigation import NavigationStore
    h = _Harness(services)
    h.message(text="/start")
    old = h.card_id()

    def unavailable(*args, **kwargs):
        raise RuntimeError("isolated navigation failure")

    monkeypatch.setattr(NavigationStore, "set", unavailable)
    with pytest.raises(RuntimeError, match="navigation failure"):
        h.press_text(texts.MENU_NEW_POST)
    assert h.card_id() == old
    assert old in h.session.visible
    # Persistent-keyboard input is a real message; on failed presentation it
    # remains, together with the keyboard installation and old working card.
    assert len(h.session.visible) == 3
    assert any(p.get("text") == texts.MENU_NEW_POST for p in h.session.visible.values())


def test_forward_source_kept_until_explicit_durable_copy(services):
    h = _Harness(services)
    h.message(text="/start")
    h.press_text(texts.MENU_NEW_POST)
    h.message(text="attributed source", message_id=40,
              forward_origin={"type": "hidden_user", "date": NOW,
                              "sender_user_name": "source"})
    assert h.ui.has_effect(f"{OWNER}:40")
    assert 40 in h.session.visible
    assert "не удаляйте" in h.last_text()
    receipts = []
    h.session.before_delete = lambda mid: receipts.append(
        (mid, h.ui.get().payload.get("staged"), services.conn.in_transaction))
    h.press_text(texts.BTN_MAKE_COPY)
    assert 40 not in h.session.visible
    receipt = next(r for r in receipts if r[0] == 40)
    assert receipt[1]["kind"] == "text"
    assert receipt[1]["text"] == "attributed source"
    assert receipt[2] is False
    h.press_text(texts.BTN_CONTINUE)
    saved = services.drafts.get_content(services.drafts.list_drafts()[0])
    assert saved.mode == "copy"
    assert saved.text == "attributed source"
    assert list(h.session.visible)[-1] == h.card_id()


@pytest.mark.parametrize("kinds", [("audio", "audio"), ("photo", "video", "audio")])
def test_incomplete_forward_copy_keeps_every_source_and_can_continue(
    tmp_path, kinds
):
    import asyncio
    from test_live_composition import live_config, make_bot, OWNER as LIVE_OWNER
    from test_ui_forward_intake import Intake, ORIGIN
    from test_ui_original_forward import NativeSession, labels
    from poster_bot.live import compose_live
    from poster_bot.repo.plans import PlanStore
    from poster_bot.ui.state import UiStateRepo

    async def scenario():
        config = live_config(tmp_path)
        session = NativeSession()
        live = await compose_live(config, clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            await h.message(message_id=2, text=texts.MENU_NEW_POST)
            mids = tuple(range(40, 40 + len(kinds)))
            for mid, kind in zip(mids, kinds):
                media = {"file_id": f"file-{mid}", "file_unique_id": f"unique-{mid}"}
                if kind in ("photo", "video"):
                    media.update(width=100, height=100)
                if kind != "photo":
                    media["duration"] = 3
                await h.message(message_id=mid, media_group_id="forward-album",
                                forward_origin=ORIGIN,
                                **{kind: [media] if kind == "photo" else media})
            before = UiStateRepo(live.app.conn).get()

            async def unavailable_copy():
                await h.press(texts.BTN_COPY_SIGNED)
                assert UiStateRepo(live.app.conn).get() == before
                assert "Невозможно" in h.card()['text']
                assert all(not h.deleted(mid) for mid in mids)
                assert not live.app.conn.execute('SELECT 1 FROM posts').fetchone()

            await unavailable_copy()
            await live.close()
            live = await compose_live(config, clock=lambda: NOW,
                                      bot=make_bot(session), now_epoch=NOW)
            h = Intake(live, session)
            assert UiStateRepo(live.app.conn).get() == before
            assert tuple(h.staged().forward_ids) == mids
            assert h.staged().forward_chat_id == LIVE_OWNER
            # Retry remains possible but cannot save a truncated logical copy.
            await unavailable_copy()
            await h.press(texts.BTN_CONTINUE)
            assert texts.BTN_COPY_SIGNED in labels(h)
            assert texts.BTN_FORWARD_AS_IS in labels(h)
            assert not live.app.conn.execute('SELECT 1 FROM posts').fetchone()
            assert not [p for m, p in session.requests if m == 'forwardMessages']
            # Continue alone is not native consent. Choose and fully preview it.
            await h.press(texts.BTN_FORWARD_AS_IS)
            assert not live.app.conn.execute('SELECT 1 FROM posts').fetchone()
            assert [p for m, p in session.requests if m == 'forwardMessages'] == [
                dict(chat_id=LIVE_OWNER, from_chat_id=LIVE_OWNER, message_ids=list(mids))]
            await h.press(texts.BTN_CONTINUE)
            assert texts.DRAFT_SAVED in h.card()['text']
            pid = UiStateRepo(live.app.conn).get().arg('post_id')
            saved = live.app.drafts.get_content(pid)
            stored = PlanStore(live.app.conn).get(pid)
            assert saved.kind == 'forward'
            assert saved.forward.message_ids == mids
            assert stored.plan.version == 4
            assert stored.plan.source.message_ids == mids
            assert stored.logical.source.message_ids == mids
            assert live.app.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == 1
            assert not live.app.conn.execute('SELECT 1 FROM publications').fetchone()
            assert any(' → ' in label for label in labels(h))
            assert all(not h.deleted(mid) for mid in mids)
        finally:
            await live.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("kinds", [("audio",), ("photo", "video")])
def test_complete_forward_copy_durable_before_cleanup_and_after_restart(
    services, tmp_path, kinds
):
    from poster_bot.domain.content import Entity
    from poster_bot.ui.staged import Staged
    from ui_flow_helpers import press, reopen

    h = _Harness(services)
    h.message(text="/start")
    h.press_text(texts.MENU_NEW_POST)
    mids = tuple(range(40, 40 + len(kinds)))
    for mid, kind in zip(mids, kinds):
        media = {"file_id": f"file-{mid}", "file_unique_id": f"unique-{mid}"}
        if kind in ("photo", "video"):
            media.update(width=100, height=100)
        if kind != "photo":
            media.update(duration=3, mime_type="audio/mpeg" if kind == "audio"
                         else "video/mp4")
        h.message(message_id=mid, caption=f"caption-{mid}",
                  caption_entities=[{"type": "bold", "offset": 0, "length": 7}],
                  media_group_id="album" if len(kinds) > 1 else None,
                  forward_origin={"type": "hidden_user", "date": NOW,
                                  "sender_user_name": "source"},
                  **{kind: [media] if kind == "photo" else media})
    assert not set(mids).intersection(deleted(h))
    receipts = []

    def before_delete(mid):
        if mid in mids:
            content = Staged.from_dict(h.ui.get().payload["staged"]).build()
            receipts.append((mid, content, services.conn.in_transaction))

    h.session.before_delete = before_delete
    h.press_text(texts.BTN_MAKE_COPY)
    assert [mid for mid, _, _ in receipts] == list(mids)
    assert all(not transaction for _, _, transaction in receipts)
    expected = receipts[0][1]
    assert all(content == expected for _, content, _ in receipts)
    assert expected.mode == "copy"
    items = expected.items if expected.kind == "album" else (expected.media,)
    assert [item.file_id for item in items] == [f"file-{mid}" for mid in mids]
    assert [item.caption for item in items] == [f"caption-{mid}" for mid in mids]
    assert all(item.caption_entities == (Entity("bold", 0, 7),) for item in items)
    token = h._button_data(texts.BTN_CONTINUE)
    restarted, flow, _ = reopen(services, tmp_path)
    created = press(flow, token, now=NOW + 60)
    assert texts.DRAFT_SAVED in created.text
    saved = restarted.drafts.get_content(restarted.drafts.list_drafts()[0])
    assert saved == expected


@pytest.mark.parametrize("flow_kind", ["settings", "manual_time", "caption"])
def test_inputs_cleaned_by_control_or_content_receipt_policy(services, flow_kind):
    h = _Harness(services)
    h.message(text="/start")
    if flow_kind == "settings":
        h.message(text=texts.MENU_SETTINGS)
        h.press_text(texts.SET_START_LABEL)
        valid, invalid = "5", "25"
    elif flow_kind == "manual_time":
        post_id = services.drafts.create(text_content(), now_epoch=NOW)
        h.flow._state.set(State.MANUAL_TIME,
                          {"purpose": "schedule", "post_id": post_id}, now_epoch=NOW)
        valid, invalid = "23:42", "25:99"
    else:
        h.press_text(texts.MENU_NEW_POST)
        h.message(message_id=30, photo=[{"file_id": "p", "file_unique_id": "u",
                                       "width": 100, "height": 100}])
        valid, invalid = "caption preserved", "x" * 4097
    if flow_kind == "caption":
        h.message(message_id=40, voice={"file_id": "v", "file_unique_id": "u",
                                        "duration": 3})
    else:
        h.message(text=invalid, message_id=40)
    if flow_kind == 'caption':
        assert 40 in h.session.visible and 40 not in deleted(h)
        assert not h.flow.input_finished(f'{OWNER}:40')
    else:
        assert 40 not in h.session.visible and 40 in deleted(h)
        assert h.flow.input_discarded(f'{OWNER}:40')
        assert not h.flow.input_captured(f'{OWNER}:40')
    h.message(text=valid, message_id=41)
    assert h.ui.has_effect(f"{OWNER}:41")
    assert 41 not in h.session.visible
    assert list(h.session.visible)[-1] == h.card_id()
    version = h.ui.get().version
    h.message(text=valid, message_id=41)  # queue replay after deletion
    assert h.ui.get().version == version
    if flow_kind == "caption":
        h.press_text(texts.BTN_AS_CAPTION)
        assert h.ui.get().payload["staged"]["media"]["caption"] == valid


def test_private_cleanup_delete_failure_is_best_effort(services):
    h = _Harness(services)
    h.message(text="/start")
    h.press_text(texts.MENU_NEW_POST)

    def fail_delete(mid):
        raise RuntimeError("isolated Telegram deletion failure")

    h.session.before_delete = fail_delete
    h.message(text="durably stored", message_id=40)
    assert h.ui.has_effect(f"{OWNER}:40")
    assert 40 in h.session.visible  # manual cleanup remains possible
    assert list(h.session.visible)[-1] == h.card_id()


def test_callback_acknowledged_before_preview_then_card_sent_last(services, monkeypatch):
    from poster_bot.transport.aiogram_channel import AiogramChannelTransport
    h = _Harness(services)
    transport = AiogramChannelTransport(h.bot, channel_id=-100555, owner_id=OWNER)
    h.message(text="/start")
    h.press_text(texts.MENU_NEW_POST)
    h.message(text="preview", message_id=40)
    original = transport.send_preview
    seen = []

    async def preview(content):
        seen.append(h.session.requests[-1][0])
        return await original(content)

    monkeypatch.setattr(transport, "send_preview", preview)
    h.flow._preview = transport
    before = len(h.session.requests)
    h.press_text(texts.BTN_CONTINUE)
    assert seen == ["answerCallbackQuery"]
    requests = h.session.requests[before:]
    assert [m for m, _ in requests].count("answerCallbackQuery") == 1
    assert [p["text"] for m, p in requests if m == "sendMessage"][0] == "preview"
    assert list(h.session.visible)[-1] == h.card_id()


def test_callback_presentation_failure_cannot_repeat_draft_creation(services):
    from aiogram.exceptions import TelegramBadRequest
    from test_ui_navigation import callback
    h = _Harness(services)
    h.message(text="/start")
    h.press_text(texts.MENU_NEW_POST)
    h.message(text="only one draft", message_id=40)
    token, old = h._button_data(texts.BTN_CONTINUE), h.card_id()
    h.session.send_error = "Bad Request: isolated send failure"
    with pytest.raises(TelegramBadRequest):
        callback(h, token, old)
    assert len(services.drafts.list_drafts()) == 1
    assert old in h.session.visible
    before = len(h.session.requests)
    callback(h, token, old)
    assert [m for m, _ in h.session.requests[before:]] == ["answerCallbackQuery"]
    assert len(services.drafts.list_drafts()) == 1
    assert h.card_id() == old
