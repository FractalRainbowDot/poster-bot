"""Draft list selection previews through the real offline Dispatcher."""
import pytest

from conftest import NOW, future
from test_ui_handlers_offline import OWNER, _Harness
from test_ui_publication_preview import callback, persistent_content
from poster_bot.domain.content import Content, Entity, ForwardSource, MediaRef
from poster_bot.transport.aiogram_channel import AiogramChannelTransport
from poster_bot.ui import texts
from ui_flow_helpers import FLOW_CHANNEL


def draft_list(services):
    content = Content(kind="text", text="draft body", entities=(
        Entity("custom_emoji", 0, 2, custom_emoji_id="123"),))
    pid = services.drafts.create(content, now_epoch=NOW)
    h = _Harness(services)
    h.flow._preview = AiogramChannelTransport(h.bot, channel_id=FLOW_CHANNEL, owner_id=OWNER)
    h.message(text=texts.MENU_DRAFTS)
    token = h._button_data(next(label for label in h.last_labels() if f"№{pid}" in label))
    return h, pid, content, token


def test_selection_previews_once_privately_before_fresh_controls(services):
    h, pid, content, token = draft_list(services)
    before = persistent_content(services)
    start = len(h.session.requests)
    callback(h, token)
    requests = h.session.requests[start:]
    assert [m for m, _ in requests[:2]] == ["answerCallbackQuery", "sendMessage"]
    assert requests[1][1]["text"] == content.text
    assert requests[1][1]["entities"][0]["custom_emoji_id"] == "123"
    assert all(p.get("chat_id", OWNER) == OWNER for _, p in requests)
    assert not any(m == "editMessageText" for m, _ in requests)
    assert texts.BTN_PREVIEW in h.last_labels()
    start = len(h.session.requests)
    callback(h, token)
    assert [m for m, _ in h.session.requests[start:]] == ["answerCallbackQuery"]
    h.press_text(texts.BTN_PREVIEW)
    assert sum(m == "sendMessage" and p.get("text") == content.text
               for m, p in h.session.requests) == 2
    assert persistent_content(services) == before


@pytest.mark.parametrize("change", ["context", "edit", "planned", "deleted", "nonowner", "forged", "ack_edit", "ack_plan"])
def test_invalid_selection_never_sends_content(services, change):
    h, pid, _, token = draft_list(services)
    if change == "context":
        h.message(text=texts.MENU_SETTINGS)
    if change == "edit":
        services.drafts.update(pid, Content(kind="text", text="changed"), now_epoch=NOW)
    if change == "planned":
        services.publications.schedule(pid, future(), now_epoch=NOW)
    if change == "deleted":
        services.drafts.delete(pid)
    if change == "ack_edit":
        h.session.before_answer = lambda: services.drafts.update(
            pid, Content(kind="text", text="changed"), now_epoch=NOW)
    if change == "ack_plan":
        h.session.before_answer = lambda: services.publications.schedule(pid, future(), now_epoch=NOW)
    start = len(h.session.requests)
    if change == "nonowner":
        h.press_text(next(label for label in h.last_labels() if f"№{pid}" in label), user_id=999)
    else:
        callback(h, "forged" if change == "forged" else token)
    assert [m for m, _ in h.session.requests[start:]] == ([] if change == "nonowner" else ["answerCallbackQuery"])


def test_failed_auto_preview_retains_draft_and_internal_back_does_not_repeat(services):
    h, pid, _, token = draft_list(services)
    before = persistent_content(services)
    h.session.send_error = "Bad Request: isolated failure"
    callback(h, token)
    assert texts.PREVIEW_FAILED_PREFIX in h.last_text()
    assert texts.BTN_PREVIEW in h.last_labels()
    assert persistent_content(services) == before
    start = len(h.session.requests)
    h.press_text(texts.BTN_DELETE)
    h.press_text(texts.BTN_BACK)
    h.press_text(texts.BTN_EDIT_CONTENT)
    h.press_text(texts.BTN_CANCEL)
    assert not any(m == "sendMessage" and p.get("text") == "draft body"
                   for m, p in h.session.requests[start:])
    assert services.drafts.is_draft(pid)


@pytest.mark.parametrize("kind,method", [
    ("photo", "sendPhoto"), ("video", "sendVideo"), ("audio", "sendAudio"),
    ("album", "sendMediaGroup"), ("forward", "forwardMessages"),
])
def test_automatic_preview_preserves_media_and_forward_sources(services, kind, method):
    entities = (Entity("custom_emoji", 0, 2, custom_emoji_id="123"),)
    if kind == "forward":
        content = Content(kind=kind, mode="forward", forward=ForwardSource(OWNER, (40, 41)))
    elif kind == "album":
        content = Content(kind=kind, items=(MediaRef("photo", "p"), MediaRef("video", "v")),
                          caption="body", caption_entities=entities)
    else:
        content = Content(kind=kind, media=MediaRef(kind, "file", "body", entities))
    pid = services.drafts.create(content, now_epoch=NOW)
    h = _Harness(services)
    h.flow._preview = AiogramChannelTransport(h.bot, channel_id=FLOW_CHANNEL, owner_id=OWNER)
    h.session.visible.update({40: {"text": "source1"}, 41: {"text": "source2"}})
    h.message(text=texts.MENU_DRAFTS)
    before = persistent_content(services)
    start = len(h.session.requests)
    h.press_text(next(label for label in h.last_labels() if f"№{pid}" in label))
    requests = h.session.requests[start:]
    assert requests[0][0] == "answerCallbackQuery"
    payload = next(p for m, p in requests if m == method)
    assert payload["chat_id"] == OWNER
    if kind == "forward":
        assert payload["message_ids"] == [40, 41]
        assert payload["from_chat_id"] == OWNER
        assert all(mid in h.session.visible for mid in (40, 41))
    else:
        media = payload["media"][0] if kind == "album" else payload
        assert media["caption"] == "body"
        assert media["caption_entities"][0]["custom_emoji_id"] == "123"
    assert persistent_content(services) == before
