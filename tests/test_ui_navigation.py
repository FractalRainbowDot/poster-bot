"""Offline Dispatcher regressions for the persistent menu/service-card split."""
import pytest

from conftest import NOW
from test_ui_handlers_offline import OWNER, _Harness, _raw_message
from ui_flow_helpers import build_flow, reopen
from poster_bot.ui.actions import ActionStore
from poster_bot.ui.state import State
from poster_bot.ui import texts


def test_start_after_local_history_clear_sends_visible_keyboard_and_card(services):
    h = _Harness(services)
    h.message(text="/start", message_id=30)
    old = h.card_id()
    h.session.visible.clear()  # server still accepts edits of old IDs
    before = len(h.session.requests)
    h.message(text="/start", message_id=31)
    assert h.card_id() != old
    assert list(h.session.visible)[-1] == h.card_id()
    requests = h.session.requests[before:]
    assert not any(m == "editMessageText" for m, _ in requests)
    assert sum(m == "sendMessage" for m, _ in requests) == 2
    assert any("keyboard" in p.get("reply_markup", {})
               for m, p in requests if m == "sendMessage")
    assert 31 not in h.session.visible


def test_intake_and_real_preview_keep_card_last_and_capture_before_delete(services):
    from poster_bot.transport.aiogram_channel import AiogramChannelTransport
    h = _Harness(services)
    h.flow._preview = AiogramChannelTransport(h.bot, channel_id=-100555, owner_id=OWNER)
    h.message(text="/start")
    h.press_text(texts.MENU_NEW_POST)
    old = h.card_id()

    receipts = []

    def durable_before_delete(mid):
        if mid == 40:
            receipts.append((h.ui.has_effect(f"{OWNER}:40"),
                             h.ui.get().payload["staged"]["text"],
                             services.conn.in_transaction))

    h.session.before_delete = durable_before_delete
    h.message(text="accepted", message_id=40)
    assert receipts == [(True, "accepted", False)]
    assert old not in h.session.visible
    assert 40 not in h.session.visible
    assert list(h.session.visible)[-1] == h.card_id()
    h.press_text(texts.BTN_CONTINUE)
    visible = list(h.session.visible.values())
    assert visible[-2]["text"] == "accepted"  # preview survives
    assert list(h.session.visible)[-1] == h.card_id()


def test_failed_replacement_preserves_old_card_and_deduplicates_intake(services):
    from aiogram.exceptions import TelegramBadRequest
    h = _Harness(services)
    h.message(text="/start")
    h.press_text(texts.MENU_NEW_POST)
    old = h.card_id()
    h.session.send_error = "Bad Request: temporary send failure"
    with pytest.raises(TelegramBadRequest):
        h.message(text="accepted", message_id=40)
    assert h.card_id() == old
    assert old in h.session.visible
    version = h.ui.get().version
    h.message(text="accepted", message_id=40)
    assert h.ui.get().version == version
    assert h.ui.get().state == State.CREATING
    assert 40 not in h.session.visible


def callback(h, token, message_id, *, user_id=OWNER, chat_id=OWNER):
    h.feed({"callback_query": {
        "id": f"cb{h._update_id}",
        "from": {"id": user_id, "is_bot": False, "first_name": "x"},
        "chat_instance": "ci", "data": token,
        "message": _raw_message(message_id=message_id, text="card",
                                chat={"id": chat_id, "type": "private"}),
    }})


def test_persistent_keyboard_reinstalled_on_start_and_cards_replaced(services):
    h = _Harness(services)
    h.message(text="/start")
    markup = h.sent()[0]["reply_markup"]
    assert markup["resize_keyboard"] is True
    assert markup["is_persistent"] is True
    assert [[b["text"] for b in row] for row in markup["keyboard"]] == [
        [texts.MENU_NEW_POST, texts.MENU_DRAFTS],
        [texts.MENU_PLAN, texts.MENU_SETTINGS],
        [texts.MENU_JOURNAL, texts.MENU_RESTART]]
    card_id = h.card_id()
    h.press_text(texts.MENU_NEW_POST)
    h.press_text(texts.BTN_CANCEL)
    h.press_text(texts.MENU_SETTINGS)
    h.message(text="/start")
    assert len(h.sent()) == 7  # two starts install menu + card, three callbacks
    edits = [p for m, p in h.session.requests if m == "editMessageText"]
    assert edits == []
    assert card_id not in h.session.visible
    assert len(h.session.visible) == 2  # latest keyboard prompt and bottom card
    assert list(h.session.visible)[-1] == h.card_id()


@pytest.mark.parametrize("label,state", [
    (texts.MENU_NEW_POST, State.CREATING), (texts.MENU_DRAFTS, State.MENU),
    (texts.MENU_PLAN, State.MENU), (texts.MENU_SETTINGS, State.SETTINGS)])
def test_exact_menu_labels_override_content_intake(services, label, state):
    h = _Harness(services)
    h.message(text="/start")
    h.message(text=texts.MENU_NEW_POST, message_id=10)
    h.message(text=label, message_id=11)
    assert h.ui.get().state == state
    assert services.drafts.list_drafts() == []
    assert 10 not in h.session.visible and 11 not in h.session.visible
    assert list(h.session.visible)[-1] == h.card_id()


@pytest.mark.parametrize("step", ["creating", "editing", "date", "settings"])
def test_restart_button_opens_today_without_changing_saved_posts(services, step):
    from conftest import future, text_content

    h = _Harness(services)
    draft = services.drafts.create(text_content("saved draft"), now_epoch=NOW)
    planned = services.drafts.create(text_content("planned post"), now_epoch=NOW)
    publication = services.publications.schedule(planned, future(minutes=120), now_epoch=NOW)
    h.message(text="/start")
    if step == "creating":
        h.message(text=texts.MENU_NEW_POST, message_id=10)
        h.message(text="unfinished input", message_id=11)
    elif step == "editing":
        h.flow.draft_edit_start(now=NOW, post_id=draft)
        h.message(text="uncommitted edit", message_id=11)
    elif step == "date":
        h.message(text=texts.MENU_DRAFTS, message_id=10)
        h.press_text(next(label for label in h.last_labels()
                          if label.startswith(f"№{draft} |")))
        h.press_text(texts.BTN_SCHEDULE)
        h.message(text="12:30", message_id=11)
        h.press_text("Изменить дату")
    else:
        h.message(text=texts.MENU_SETTINGS, message_id=10)
        h.press_text(texts.SET_START_LABEL)
    old_card = h.card_id()
    old_keyboard = h.flow.navigation_store(OWNER).keyboard_message(OWNER)
    settings = services.settings.get()
    assert h.preview.calls == []  # setup must never publish to the channel
    previews_before = list(h.preview.owner_previews)
    assert len(previews_before) == (1 if step == "date" else 0)
    lifecycle = h.flow.navigation_store(OWNER).lifecycle._load()
    preview_ids = set(lifecycle['active'] + lifecycle['candidate'])
    preview_ids.update(int(mid) for mid in lifecycle['pending'])
    h.session.visible.clear()  # locally cleared history must be recreated
    before = len(h.session.requests)
    h.message(text="Рестарт", message_id=50)
    assert h.ui.get().state is State.MENU
    assert h.ui.get().payload == {"view": "day", "day": "2026-10-21", "page": 0}
    requests = h.session.requests[before:]
    sent = [p for m, p in requests if m == "sendMessage"]
    assert len(sent) == 2  # fresh persistent keyboard, then fresh day plan
    assert sent[0]["reply_markup"]["keyboard"][-1] == [
        {"text": texts.MENU_JOURNAL}, {"text": texts.MENU_RESTART}]
    assert sent[0]["reply_markup"]["is_persistent"] is True
    assert sent[1]["reply_markup"]["inline_keyboard"]
    assert all(p.get("text") != "/start" for p in sent)
    assert all(p["chat_id"] == OWNER for _, p in requests)
    assert {m for m, _ in requests} == {"sendMessage", "deleteMessage"}
    assert h.card_id() != old_card
    assert h.flow.navigation_store(OWNER).keyboard_message(OWNER) != old_keyboard
    assert len(h.session.visible) == 2
    assert list(h.session.visible)[-1] == h.card_id()
    assert {p["message_id"] for m, p in requests if m == "deleteMessage"} == {
        old_card, old_keyboard, 50} | preview_ids
    assert services.drafts.get_content(draft) == text_content("saved draft")
    assert services.drafts.get_content(planned) == text_content("planned post")
    assert services.drafts.list_drafts() == [draft]
    assert services.publications.get(publication.id) == publication
    assert services.settings.get() == settings
    assert h.preview.calls == []
    assert h.preview.owner_previews == previews_before  # Restart adds no preview
    assert not h.ui.has_effect(f"{OWNER}:50")


@pytest.mark.parametrize("user_id,chat_id", [(999, OWNER), (OWNER, -555), (OWNER, 999)])
def test_restart_button_denied_outside_owner_private_chat(services, user_id, chat_id):
    h = _Harness(services)
    h.message(text="Рестарт", chat={"id": chat_id, "type": "private" if chat_id > 0 else "group"},
              **{"from": {"id": user_id, "is_bot": False, "first_name": "x"}})
    assert h.session.requests == []
    assert h.ui.get().version == 0


@pytest.mark.parametrize("label", ["Рестарт ", "Рестартовать", "/startlater"])
def test_restart_and_start_prefixes_remain_content(services, label):
    h = _Harness(services)
    h.message(text="/start")
    h.message(text=texts.MENU_NEW_POST, message_id=10)
    h.message(text=label, message_id=20)
    assert h.ui.get().state is State.CREATING
    assert h.ui.get().payload["staged"]["text"] == label


def test_forwarded_restart_label_stays_source_not_menu_command(services):
    h = _Harness(services)
    h.message(text="/start")
    h.message(text=texts.MENU_NEW_POST, message_id=10)
    h.message(text="Рестарт", message_id=20,
              forward_origin={"type": "hidden_user", "date": NOW,
                              "sender_user_name": "source"})
    assert h.ui.get().state is State.CREATING
    assert h.ui.get().payload["staged"]["kind"] == "forward"
    assert 20 in h.session.visible
    h.message(text="Рестарт", message_id=21)
    assert h.ui.get().state is State.MENU
    assert 20 in h.session.visible  # resetting UI must not delete forward sources


def test_restart_button_cleanup_failure_is_best_effort(services):
    h = _Harness(services)
    h.message(text="/start")
    h.message(text=texts.MENU_NEW_POST, message_id=10)

    def fail_delete(mid):
        raise RuntimeError("isolated deletion failure")

    h.session.before_delete = fail_delete
    h.message(text="Рестарт", message_id=20)
    assert h.ui.get().state is State.MENU
    assert list(h.session.visible)[-1] == h.card_id()
    assert h.session.visible[h.card_id()]["reply_markup"]["inline_keyboard"]
    assert 20 in h.session.visible


def test_content_forward_and_preview_never_deleted_or_used_as_card(services):
    h = _Harness(services)
    h.message(text="/start")
    h.message(text=texts.MENU_NEW_POST, message_id=10)
    h.message(text=texts.MENU_SETTINGS, message_id=20,
              forward_origin={"type": "hidden_user", "date": NOW,
                              "sender_user_name": "source"})
    assert h.ui.get().state == State.CREATING
    h.press_text(texts.BTN_FORWARD_AS_IS)
    assert len(h.preview.owner_previews) == 1
    assert 20 in h.session.visible  # forwardMessage still needs this source
    assert 10 not in h.session.visible
    assert list(h.session.visible)[-1] == h.card_id()
    assert not any(m == "editMessageText" for m, _ in h.session.requests)


def test_accepted_text_and_pending_media_deleted_after_capture(services):
    h = _Harness(services)
    h.message(text="/start")
    h.press_text(texts.MENU_NEW_POST)
    h.message(text="Настройки ", message_id=20)
    h.message(message_id=21, photo=[{"file_id": "p", "file_unique_id": "u",
                                    "width": 100, "height": 100}])
    assert h.ui.has_effect(f"{OWNER}:20")
    assert h.ui.has_effect(f"{OWNER}:21")
    assert 20 not in h.session.visible and 21 not in h.session.visible
    assert "pending_replace" in h.ui.get().payload


def test_replacement_does_not_retry_flow(services):
    h = _Harness(services)
    h.message(text="/start")
    old = h.card_id()
    h.session.edit_error = "Bad Request: message to edit not found"
    version = h.ui.get().version
    h.press_text(texts.MENU_NEW_POST)
    assert h.ui.get().version == version + 1
    assert h.ui.get().state == State.CREATING
    assert h.card_id() != old
    assert len(h.sent()) == 3
    assert old not in h.session.visible
    assert list(h.session.visible)[-1] == h.card_id()


def test_edit_response_cannot_prevent_new_bottom_card(services):
    h = _Harness(services)
    h.message(text="/start")
    h.session.edit_error = "Bad Request: message is not modified"
    h.press_text(texts.MENU_NEW_POST)
    assert h.ui.get().state == State.CREATING
    assert len(h.sent()) == 3
    assert not any(m == "editMessageText" for m, _ in h.session.requests)


def test_failed_send_preserves_only_working_card(services):
    from aiogram.exceptions import TelegramBadRequest
    h = _Harness(services)
    h.message(text="/start")
    old = h.card_id()
    h.session.send_error = "Bad Request: can't parse entities"
    with pytest.raises(TelegramBadRequest):
        h.press_text(texts.MENU_NEW_POST)
    assert h.card_id() == old
    assert old in h.session.visible
    assert h.ui.get().state == State.CREATING


def test_restart_recognizes_durable_card_then_replaces_and_reinstalls(services, tmp_path):
    h = _Harness(services)
    h.message(text="/start")
    token = h.flow._token(h.flow.A_JOURNAL, now=NOW)  # previously issued main-card button
    old = h.card_id()
    # New SQLite connection, flow and Dispatcher over the isolated database.
    fresh, _, _ = reopen(services, tmp_path)
    restarted = _Harness(fresh)
    restarted.session._next_id = h.session._next_id
    restarted.session.visible = dict(h.session.visible)
    callback(restarted, token, old)
    assert restarted.ui.get().payload == {"view": "journal"}
    assert len(restarted.sent()) == 1
    assert old not in restarted.session.visible
    before = len(restarted.session.requests)
    callback(restarted, token, old)
    assert [m for m, _ in restarted.session.requests[before:]] == ["answerCallbackQuery"]
    restarted.message(text=texts.MENU_SETTINGS, message_id=40)
    restarted.message(text="/start")
    assert len(restarted.sent()) == 4
    assert restarted.card_id() != old
    assert len(restarted.session.visible) == 2
    assert list(restarted.session.visible)[-1] == restarted.card_id()
    fresh.conn.close()


def test_real_preview_transport_keeps_preview_separate_from_service_card(services):
    from poster_bot.transport.aiogram_channel import AiogramChannelTransport
    h = _Harness(services)
    h.flow._preview = AiogramChannelTransport(h.bot, channel_id=-100555, owner_id=OWNER)
    h.message(text="/start")
    card = h.card_id()
    h.press_text(texts.MENU_NEW_POST)
    h.message(text="Preview content must remain", message_id=20)
    h.press_text(texts.BTN_CONTINUE)
    preview = [p for p in h.sent() if p["text"] == "Preview content must remain"]
    assert len(preview) == 1
    assert preview[0]["chat_id"] == OWNER
    assert h.card_id() != card
    assert card not in h.session.visible
    assert 20 not in h.session.visible
    assert list(h.session.visible.values())[-2]["text"] == "Preview content must remain"
    assert list(h.session.visible)[-1] == h.card_id()


@pytest.mark.parametrize("user_id,chat_id", [(999, OWNER), (OWNER, -555)])
def test_denied_callback_no_output_and_no_token_lookup(services, user_id, chat_id):
    h = _Harness(services)
    h.message(text="/start")
    token = h.flow._token(h.flow.A_JOURNAL, now=NOW)
    before = list(h.session.requests)
    callback(h, token, h.card_id(), user_id=user_id, chat_id=chat_id)
    assert h.session.requests == before
    assert ActionStore(services.conn).peek(token).status == "pending"


def test_unknown_card_cannot_consume_token_or_edit_preview(services):
    h = _Harness(services)
    h.message(text="/start")
    token = h.flow._token(h.flow.A_JOURNAL, now=NOW)
    before = len(h.session.requests)
    callback(h, token, 12345)
    assert [m for m, _ in h.session.requests[before:]] == ["answerCallbackQuery"]
    assert h.ui.get().state == State.MENU
    assert ActionStore(services.conn).peek(token).status == "pending"


def test_service_tracking_owner_filtered(services):
    flow, _ = build_flow(services)
    one = flow.navigation_store(111)
    one.set(111, 900, now=NOW)
    assert one.get(222) is None
    assert flow.navigation_store(222).get(111) is None


@pytest.mark.parametrize("expired_answer", [False, True])
def test_duplicate_token_only_toasts_and_preserves_active_card(
        services, monkeypatch, expired_answer):
    h = _Harness(services)
    h.message(text="/start")
    token = h.flow._token(h.flow.A_JOURNAL, now=NOW)
    calls = []
    original = h.flow.journal_view

    def journal_view(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(h.flow, "journal_view", journal_view)
    callback(h, token, h.card_id())
    assert len(calls) == 1
    assert h.ui.get().payload == {"view": "journal"}
    card, state, card_id = h.cards()[-1], h.ui.get(), h.card_id()
    assert card["reply_markup"]["inline_keyboard"]
    before = len(h.session.requests)
    if expired_answer:
        h.session.answer_error = "Bad Request: query is too old"
    callback(h, token, card_id)
    assert [m for m, _ in h.session.requests[before:]] == ["answerCallbackQuery"]
    assert h.session.requests[-1][1]["text"] == texts.BUTTON_STALE
    assert h.cards()[-1] == card
    assert h.card_id() == card_id
    assert h.ui.get() == state
    assert len(calls) == 1


def test_old_timezone_token_after_pagination_only_toasts(services):
    h = _Harness(services)
    h.message(text="/start")
    h.press_text(texts.MENU_SETTINGS)
    h.press_text(texts.SET_TZ_LABEL)
    card = h.cards()[-1]
    old_token = card["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    h.press_text("След. →")
    card, state, card_id = h.cards()[-1], h.ui.get(), h.card_id()
    settings = services.settings.get()
    before = len(h.session.requests)
    callback(h, old_token, card_id)
    assert [m for m, _ in h.session.requests[before:]] == ["answerCallbackQuery"]
    assert h.session.requests[-1][1]["text"] == texts.BUTTON_STALE
    assert h.cards()[-1] == card
    assert h.card_id() == card_id
    assert h.ui.get() == state
    assert services.settings.get() == settings
