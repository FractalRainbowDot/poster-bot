"""Private fresh previews through real Dispatcher and offline aiogram transport."""
import pytest

from conftest import NOW, future
from test_ui_handlers_offline import OWNER, _Harness, _raw_message
from ui_flow_helpers import FLOW_CHANNEL
from poster_bot.domain.content import Content, Entity, ForwardSource, MediaRef
from poster_bot.domain.states import Status
from poster_bot.transport.aiogram_channel import AiogramChannelTransport
from poster_bot.ui import texts


def open_publication(services, content):
    pid = services.drafts.create(content, now_epoch=NOW)
    pub = services.publications.schedule(pid, future(), now_epoch=NOW)
    h = _Harness(services)
    h.flow._preview = AiogramChannelTransport(
        h.bot, channel_id=FLOW_CHANNEL, owner_id=OWNER)
    h.message(text=texts.MENU_PLAN)
    h.press_text(next(label for label in h.last_labels() if " | 🕒 | " in label))
    return h, pub


def callback(h, token):
    h.feed({"callback_query": {
        "id": "preview", "from": {"id": OWNER, "is_bot": False, "first_name": "x"},
        "chat_instance": "ci", "data": token,
        "message": _raw_message(message_id=h.card_id(), text="card"),
    }})


def persistent_content(services):
    return {table: [tuple(row) for row in services.conn.execute(f"SELECT * FROM {table}")]
            for table in ("posts", "publications", "send_attempts",
                          "published_history", "journal_events")}


@pytest.mark.parametrize("kind,method", [
    ("text", "sendMessage"), ("photo", "sendPhoto"),
    ("video", "sendVideo"), ("audio", "sendAudio"),
])
def test_scheduled_preview_fresh_content_private_and_unchanged(services, kind, method):
    entities = (Entity("bold", 0, 4),)
    content = (Content(kind="text", text="body", entities=entities) if kind == "text"
               else Content(kind=kind, media=MediaRef(
                   kind, "file", caption="body", caption_entities=entities)))
    h, pub = open_publication(services, content)
    before_db = persistent_content(services)
    old_card = h.card_id()
    for _ in range(2):
        start = len(h.session.requests)
        h.press_text(texts.BTN_PREVIEW)
        requests = h.session.requests[start:]
        assert requests[0][0] == "answerCallbackQuery"
        assert requests[1][0] == method
        payload = requests[1][1]
        assert payload["chat_id"] == OWNER
        assert payload["text" if kind == "text" else "caption"] == "body"
        assert payload["entities" if kind == "text" else "caption_entities"][0]["type"] == "bold"
        if kind != "text":
            assert payload[kind] == "file"
        assert all(m != "editMessageText" for m, _ in requests)
        assert all(p.get("chat_id", OWNER) == OWNER for _, p in requests)
        assert h.card_id() > old_card
        old_card = h.card_id()
        assert services.publications.get(pub.id) == pub
        assert services.dispatch.content_for_publication(pub.id) == content
        assert h.preview.calls == []
    assert persistent_content(services) == before_db


@pytest.mark.parametrize("status", list(Status))
def test_preview_status_policy_revalidated_before_ack(services, status):
    h, pub = open_publication(services, Content(kind="text", text="body"))
    token = h._button_data(texts.BTN_PREVIEW)
    services.conn.execute("UPDATE publications SET status=? WHERE id=?", (status.value, pub.id))
    services.conn.commit()
    start = len(h.session.requests)
    callback(h, token)
    requests = h.session.requests[start:]
    if status is Status.SENDING:
        assert requests == [("answerCallbackQuery", {
            "callback_query_id": "preview", "text": texts.BUTTON_STALE})]
    else:
        assert any(m == "sendMessage" and p.get("text") == "body" for m, p in requests)
    assert services.publications.get(pub.id).status is status


@pytest.mark.parametrize("mismatch", ["id", "version"])
def test_preview_stale_capability_cannot_target_other_publication(services, mismatch):
    h, pub = open_publication(services, Content(kind="text", text="body"))
    token = h._button_data(texts.BTN_PREVIEW)
    if mismatch == "id":
        from poster_bot.ui.state import State
        h.flow._state.set(State.PUBLICATION, {"pub_id": pub.id + 1}, now_epoch=NOW)
    else:
        services.publications.pause(pub.id, pub.version, now_epoch=NOW)
    start = len(h.session.requests)
    callback(h, token)
    assert [m for m, _ in h.session.requests[start:]] == ["answerCallbackQuery"]
    assert h.session.requests[-1][1]["text"] == texts.BUTTON_STALE


def test_preview_failure_keeps_publication_and_offers_retry(services):
    h, pub = open_publication(services, Content(kind="text", text="body"))
    before = persistent_content(services)
    h.session.send_error = "Bad Request: isolated failure"
    h.press_text(texts.BTN_PREVIEW)
    assert texts.PREVIEW_FAILED_PREFIX in h.last_text()
    assert texts.PUB_PREVIEW_RETRY_HINT in h.last_text()
    assert persistent_content(services) == before
    h.press_text(texts.BTN_PREVIEW)
    assert texts.PREVIEW_SENT in h.last_text()
    assert services.publications.get(pub.id) == pub


def test_forward_preview_keeps_sources(services):
    content = Content(kind="forward", mode="forward",
                      forward=ForwardSource(OWNER, (40, 41)))
    h, pub = open_publication(services, content)
    h.session.visible[40] = {"text": "source1"}
    h.session.visible[41] = {"text": "source2"}
    before = persistent_content(services)
    start = len(h.session.requests)
    h.press_text(texts.BTN_PREVIEW)
    requests = h.session.requests[start:]
    payload = next(p for m, p in requests if m == "forwardMessages")
    assert payload["chat_id"] == payload["from_chat_id"] == OWNER
    assert payload["message_ids"] == [40, 41]
    assert all(mid in h.session.visible for mid in (40, 41))
    assert persistent_content(services) == before
    assert services.publications.get(pub.id) == pub


def test_album_preview_preserves_caption_entities_and_items(services):
    content = Content(kind="album", items=(MediaRef("photo", "p"), MediaRef("video", "v")),
                      caption="body", caption_entities=(Entity("italic", 0, 4),))
    h, pub = open_publication(services, content)
    start = len(h.session.requests)
    h.press_text(texts.BTN_PREVIEW)
    payload = next(p for m, p in h.session.requests[start:] if m == "sendMediaGroup")
    assert payload["chat_id"] == OWNER
    assert [m["media"] for m in payload["media"]] == ["p", "v"]
    assert payload["media"][0]["caption"] == "body"
    assert payload["media"][0]["caption_entities"][0]["type"] == "italic"
    assert services.publications.get(pub.id) == pub


def test_draft_preview_always_sends_new_content_below_old_preview(services):
    pid = services.drafts.create(Content(kind="text", text="draft body"), now_epoch=NOW)
    h = _Harness(services)
    h.flow._preview = AiogramChannelTransport(h.bot, channel_id=FLOW_CHANNEL, owner_id=OWNER)
    h.message(text=texts.MENU_DRAFTS)
    h.press_text(next(label for label in h.last_labels() if f"№{pid}" in label))
    old_card = h.card_id()
    for _ in range(2):
        start = len(h.session.requests)
        h.press_text(texts.BTN_PREVIEW)
        requests = h.session.requests[start:]
        assert requests[1][0] == "sendMessage"
        assert requests[1][1]["text"] == "draft body"
        assert requests[1][1]["chat_id"] == OWNER
        assert not any(m == "editMessageText" for m, _ in requests)
        assert h.card_id() > old_card
        old_card = h.card_id()


@pytest.mark.parametrize("status", [Status.SENDING])
def test_sending_view_does_not_offer_preview(services, status):
    h, pub = open_publication(services, Content(kind="text", text="body"))
    services.conn.execute("UPDATE publications SET status=? WHERE id=?", (status.value, pub.id))
    services.conn.commit()
    reply = h.flow.pub_open(now=NOW, pub_id=pub.id)
    assert texts.BTN_PREVIEW not in [b.label for row in reply.rows for b in row]


def test_nonowner_cannot_consume_preview(services):
    h, pub = open_publication(services, Content(kind="text", text="body"))
    before = len(h.session.requests)
    h.press_text(texts.BTN_PREVIEW, user_id=999)
    assert len(h.session.requests) == before
    h.press_text(texts.BTN_PREVIEW)
    assert texts.PREVIEW_SENT in h.last_text()
    assert services.publications.get(pub.id) == pub


@pytest.mark.parametrize('menu', [texts.MENU_PLAN, '/start'])
def test_row_click_immediately_sends_one_private_preview_before_detail(services, menu):
    pid = services.drafts.create(Content(kind='text', text='row body'), now_epoch=NOW)
    pub = services.publications.schedule(pid, future(), now_epoch=NOW)
    h = _Harness(services)
    h.flow._preview = AiogramChannelTransport(h.bot, channel_id=FLOW_CHANNEL, owner_id=OWNER)
    h.message(text=menu)
    before_db = persistent_content(services)
    label = '21.10 13:00 | 🕒 | 📝 row body' if menu == '/start' else '13:00 | 🕒 | row body'
    token = h._button_data(next(value for value in h.last_labels() if label in value))
    start = len(h.session.requests)
    callback(h, token)
    requests = h.session.requests[start:]
    assert requests[0][0] == 'answerCallbackQuery'
    previews = [p for m, p in requests if m == 'sendMessage' and p.get('text') == 'row body']
    assert len(previews) == 1 and previews[0]['chat_id'] == OWNER
    assert texts.BTN_PREVIEW in h.last_labels()
    assert persistent_content(services) == before_db
    start = len(h.session.requests)
    callback(h, token)
    assert [m for m, p in h.session.requests[start:]] == ['answerCallbackQuery']
    start = len(h.session.requests)
    h.flow.pub_open(now=NOW, pub_id=pub.id)
    assert h.session.requests[start:] == []


def test_published_history_row_previews_readonly_and_never_touches_channel(services):
    content = Content(kind='text', text='published body')
    pid = services.drafts.create(content, now_epoch=NOW)
    pub = services.publications.schedule(pid, future(), now_epoch=NOW)
    attempt = services.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch)
    services.dispatch.complete(pub.id, attempt, (999,), 'https://t.me/c/555/999',
                               now_epoch=pub.scheduled_epoch)
    h = _Harness(services)
    h.flow._preview = AiogramChannelTransport(h.bot, channel_id=FLOW_CHANNEL, owner_id=OWNER)
    h.message(text=texts.MENU_PLAN)
    before = persistent_content(services)
    start = len(h.session.requests)
    h.press_text(next(label for label in h.last_labels() if ' | ✅ | published body' in label))
    requests = h.session.requests[start:]
    assert any(m == 'sendMessage' and p.get('text') == 'published body' for m, p in requests)
    assert all(p.get('chat_id', OWNER) == OWNER for m, p in requests)
    assert set(h.last_labels()) == {texts.BTN_PREVIEW, texts.BTN_LINK, texts.BTN_BACK}
    h.press_text(texts.BTN_PREVIEW)
    assert persistent_content(services) == before


@pytest.mark.parametrize('mismatch', ['version', 'context', 'nonowner'])
def test_row_capability_validated_before_any_preview_output(services, mismatch):
    from poster_bot.ui.state import State
    pid = services.drafts.create(Content(kind='text', text='body'), now_epoch=NOW)
    pub = services.publications.schedule(pid, future(), now_epoch=NOW)
    h = _Harness(services)
    h.flow._preview = AiogramChannelTransport(h.bot, channel_id=FLOW_CHANNEL, owner_id=OWNER)
    h.message(text=texts.MENU_PLAN)
    label = next(label for label in h.last_labels() if ' | 🕒 | body' in label)
    if mismatch == 'version':
        services.publications.pause(pub.id, pub.version, now_epoch=NOW)
    if mismatch == 'context':
        h.flow._state.set(State.DRAFT, {'post_id': pid}, now_epoch=NOW)
    start = len(h.session.requests)
    h.press_text(label, user_id=999 if mismatch == 'nonowner' else OWNER)
    requests = h.session.requests[start:]
    assert requests == [] if mismatch == 'nonowner' else [m for m, _ in requests] == ['answerCallbackQuery']


def test_row_preview_failure_keeps_readonly_detail_and_retry_button(services):
    pid = services.drafts.create(Content(kind='text', text='body'), now_epoch=NOW)
    pub = services.publications.schedule(pid, future(), now_epoch=NOW)
    h = _Harness(services)
    h.flow._preview = AiogramChannelTransport(h.bot, channel_id=FLOW_CHANNEL, owner_id=OWNER)
    h.message(text=texts.MENU_PLAN)
    before = persistent_content(services)
    h.session.send_error = 'Bad Request: missing original source'
    h.press_text(next(label for label in h.last_labels() if ' | 🕒 | body' in label))
    assert texts.PREVIEW_FAILED_PREFIX in h.last_text()
    assert texts.BTN_PREVIEW in h.last_labels()
    assert persistent_content(services) == before


def test_internal_cancel_back_and_pause_do_not_send_extra_preview(services):
    h, pub = open_publication(services, Content(kind='text', text='body'))
    start = len(h.session.requests)
    h.press_text(texts.BTN_TO_DRAFTS)
    h.press_text(texts.BTN_BACK)
    h.press_text(texts.BTN_PAUSE)
    assert not any(m == 'sendMessage' and p.get('text') == 'body'
                   for m, p in h.session.requests[start:])
    assert services.publications.get(pub.id).status is Status.SUSPENDED


def test_row_version_changed_during_ack_never_sends_payload(services):
    pid = services.drafts.create(Content(kind='text', text='body'), now_epoch=NOW)
    pub = services.publications.schedule(pid, future(), now_epoch=NOW)
    h = _Harness(services)
    h.flow._preview = AiogramChannelTransport(h.bot, channel_id=FLOW_CHANNEL, owner_id=OWNER)
    h.message(text=texts.MENU_PLAN)
    h.session.before_answer = lambda: services.publications.pause(pub.id, pub.version, now_epoch=NOW)
    start = len(h.session.requests)
    h.press_text(next(label for label in h.last_labels() if ' | 🕒 | body' in label))
    assert [m for m, p in h.session.requests[start:]] == ['answerCallbackQuery']
