"""Candidate approval requires complete private presentation, including controls."""
import json

import pytest
from aiogram.exceptions import TelegramBadRequest

from poster_bot.domain.content import Content, MediaRef
from poster_bot.domain.planner import plan_post
from poster_bot.domain.time import Instant
from poster_bot.ui import texts
from test_ui_edit_modes import MODES, editing, labels
from test_ui_handlers_offline import OWNER, _raw_message
from test_ui_live_preview import FailingApi, fail_sends
from test_ui_preview_lifecycle import sized_harness


class PartialAlbumApi(FailingApi):
    partial_next = True

    async def make_request(self, bot, method, timeout=None):
        if method.__api_method__ == 'sendMediaGroup' and self.partial_next:
            self.partial_next = False
            payload = method.model_dump(mode='json', exclude_none=True,
                                        exclude={'default'}, fallback=str)
            self.requests.append(('sendMediaGroup', payload))
            raw = self._raw()
            self.visible[raw['message_id']] = payload
            self.roles[raw['message_id']] = 'preview'
            return self.check_response(bot=bot, method=method, status_code=200,
                content=json.dumps({'ok': True, 'result': [raw]})).result
        return await super().make_request(bot, method, timeout)


def press_token(h, token, suffix):
    h.feed({'callback_query': {'id': suffix, 'from': {
        'id': OWNER, 'is_bot': False, 'first_name': 'owner'},
        'chat_instance': 'ci', 'data': token,
        'message': _raw_message(message_id=h.card_id(), text='card')}})


def unsigned_album_editing(services):
    h = sized_harness(services)
    content = Content(kind='album', items=(MediaRef(kind='photo', file_id='p'),
                                          MediaRef(kind='video', file_id='v')),
                      caption='original')
    pid = services.drafts.create(content, now_epoch=h.now)
    h.message(text='/start')
    h.press_text(texts.MENU_DRAFTS)
    h.press_text(labels(h)[0])
    h.press_text(texts.BTN_EDIT_CONTENT)
    return h, pid, content


@pytest.mark.parametrize('unsigned', [False, True])
@pytest.mark.parametrize('scheduled', [False, True])
def test_raw_forward_recovery_requires_copy_then_separate_edit_confirmation(services, unsigned, scheduled):
    from poster_bot.domain.content import ForwardSource
    from test_ui_live_preview import live_harness
    h = sized_harness(services) if unsigned else live_harness(services)
    original = Content(kind='forward', mode='forward',
                       forward=ForwardSource(chat_id=OWNER, message_ids=(50,)))
    other = services.drafts.create(original, now_epoch=h.now)
    pid = services.drafts.create(original, now_epoch=h.now)
    pub = services.publications.schedule(pid, Instant(h.now + 3600), now_epoch=h.now) if scheduled else None
    if scheduled:
        h.press_text(texts.MENU_PLAN)
        h.press_text(next(label for label in labels(h) if label.startswith('✉') or 'перес' in label.lower()))
    else:
        h.press_text(texts.MENU_DRAFTS)
        h.press_text(labels(h)[0])
    if unsigned:
        h.press_text(texts.BTN_EDIT_CONTENT)
    else:
        assert h.last_text() == texts.FORWARD_RESEND_ASK
        h.press_text(texts.BTN_FORWARD_RESEND)
    h.message(message_id=50, text='recovered body', forward_origin={
        'type': 'hidden_user', 'date': h.now, 'sender_user_name': 'source'})
    h.press_text(texts.BTN_CONTINUE)
    assert services.drafts.get_content(pid) == original
    assert 50 in h.session.visible
    h.press_text(h.flow._copy_btn_label())
    assert 50 in h.session.visible  # both saved references still protect it
    h.press_text(texts.BTN_CONTINUE)
    assert services.drafts.get_content(pid) == original
    if pub:
        assert services.publications.get(pub.id) == pub
    assert texts.BTN_CONFIRM in labels(h)
    h.press_text(texts.BTN_CONFIRM)
    assert services.drafts.get_content(pid).kind == 'text'
    assert services.drafts.get_content(other) == original
    assert 50 in h.session.visible
    assert h.flow.forward_source_referenced(chat_id=OWNER, message_id=50)
    if pub:
        updated = services.publications.get(pub.id)
        assert updated.version == pub.version + 1
        assert (updated.id, updated.post_id, updated.scheduled_epoch) == (pub.id, pid, pub.scheduled_epoch)


@pytest.mark.parametrize('unsigned', [False, True])
@pytest.mark.parametrize('failure', ['definitive', 'partial_album', 'controls'])
@pytest.mark.parametrize('scheduled', [False, True])
def test_failed_candidate_cannot_continue_or_confirm_until_explicit_retry(
        services, unsigned, failure, scheduled):
    h, pid, _ = unsigned_album_editing(services) if unsigned else editing(services)
    pub = None
    if scheduled:
        pub = services.publications.schedule(pid, Instant(h.now + 3600), now_epoch=h.now)
        h.press_text(texts.MENU_PLAN)
        h.press_text(next(label for label in labels(h) if 'original' in label))
        h.press_text(texts.BTN_EDIT_CONTENT)
    h.press_text(MODES[0])
    h.message(message_id=50, text='first')
    h.press_text(texts.BTN_CONTINUE)
    stale_confirm = h._button_data(texts.BTN_CONFIRM)
    old = h.session.alive('preview')
    before = dict(services.conn.execute('SELECT * FROM posts WHERE id=?', (pid,)).fetchone())
    plan_before = h.flow._plans.get(pid) if not unsigned else None
    if failure == 'partial_album':
        h.session.__class__ = PartialAlbumApi
    else:
        fail_sends(h, 1 if failure == 'definitive' else 2)
    if failure == 'controls':
        with pytest.raises(TelegramBadRequest):
            h.message(message_id=51, text='second')
    else:
        h.message(message_id=51, text='second')
    assert old.issubset(h.session.visible)
    assert h.flow.input_captured(f'{OWNER}:51')
    data = h.flow._navigation.lifecycle._load()
    known = set(data['active'] + data['candidate']) | {int(mid) for mid in data['pending']}
    assert h.session.alive('preview').issubset(known)
    # A server-issued Continue capability for this version must remain harmless
    # even if obtained before presentation completed (or an old UI left it visible).
    current_continue = h.flow._token(h.flow.A_CONTINUE, now=h.now,
                                     expected_version=h.flow._ui().version)
    failed_previews = h.session.alive('preview')
    issued_previews = {mid for mid, role in h.session.roles.items() if role == 'preview'}
    press_token(h, stale_confirm, 'stale-confirm')
    press_token(h, current_continue, 'failed-continue')
    if texts.BTN_CONFIRM in labels(h):
        h.press_text(texts.BTN_CONFIRM)
    # Failed controls can leave ACKed parts awaiting cleanup. The next user
    # transition may delete them, but must never send another preview implicitly.
    assert h.session.alive('preview').issubset(failed_previews)
    assert {mid for mid, role in h.session.roles.items() if role == 'preview'} == issued_previews
    assert texts.BTN_CONFIRM not in labels(h)
    assert texts.BTN_CONTINUE not in labels(h)
    assert old.issubset(h.session.visible)
    assert dict(services.conn.execute('SELECT * FROM posts WHERE id=?', (pid,)).fetchone()) == before
    if pub:
        assert services.publications.get(pub.id) == pub
    if not unsigned:
        assert h.flow._plans.get(pid) == plan_before
    previews = h.session.alive('preview')
    h.message(message_id=51, text='second')
    assert h.session.alive('preview') == previews
    assert h.flow._staged(h.flow._ui()).text == 'first\n\nsecond'
    h.press_text('Повторить предпросмотр')
    assert not old.intersection(h.session.visible)
    h.press_text(texts.BTN_CONTINUE)
    assert dict(services.conn.execute('SELECT * FROM posts WHERE id=?', (pid,)).fetchone()) == before
    h.press_text(texts.BTN_CONFIRM)
    content = services.drafts.get_content(pid)
    assert (content.caption if unsigned else h.flow._plans.get(pid).logical.body) == 'first\n\nsecond'
    if pub:
        updated = services.publications.get(pub.id)
        assert updated.version == pub.version + 1
        assert (updated.post_id, updated.scheduled_epoch) == (pub.post_id, pub.scheduled_epoch)


@pytest.mark.parametrize('failed_first', [False, True])
def test_candidate_plan_is_frozen_through_retry_and_confirmation(services, failed_first):
    from dataclasses import replace
    h, pid, _ = editing(services)
    h.press_text(MODES[0])
    signature = h.flow._signature
    if failed_first:
        fail_sends(h, 1)
    h.message(message_id=50, text='replacement')
    pair = h.flow._plans.get(pid)
    expected = plan_post(
        replace(pair.logical, body='replacement', entities=()), signature).plan
    h.flow._signature = replace(signature, main_text='ambient-changed')
    if failed_first:
        h.press_text('Повторить предпросмотр')
    h.press_text(texts.BTN_CONTINUE)
    h.flow._signature = replace(signature, main_text='changed-again')
    h.press_text(texts.BTN_CONFIRM)
    assert h.flow._plans.get(pid).plan.to_json() == expected.to_json()


@pytest.mark.parametrize('unsigned', [False, True])
def test_preupgrade_edit_without_mode_cannot_bypass_approval(services, unsigned):
    h, pid, _ = unsigned_album_editing(services) if unsigned else editing(services)
    h.press_text(MODES[0])
    h.message(message_id=50, text='replacement')
    before = services.drafts.get_content(pid)
    ui = h.flow._ui()
    legacy = {k: v for k, v in ui.payload.items() if not k.startswith('edit_')}
    h.flow._state.set(ui.state, legacy, expected_version=ui.version, now_epoch=h.now)
    token = h.flow._token(h.flow.A_CONTINUE, now=h.now)
    press_token(h, token, 'legacy-continue')
    assert services.drafts.get_content(pid) == before
