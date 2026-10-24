"""Prepared creation previews through the real offline Dispatcher/transport."""
import pytest
from aiogram.exceptions import TelegramBadRequest

from poster_bot.domain.signature import SignatureConfig
from poster_bot.repo.plans import PlanStore
from poster_bot.ui import texts
from test_ui_preview_lifecycle import sized_harness, SizedVisibleApi
from test_ui_handlers_offline import OWNER


def live_harness(services):
    h = sized_harness(services)
    h.flow._signature = SignatureConfig('main', 'https://example.com/main',
                                       'partner', 'https://example.com/partner')
    h.flow._plans = PlanStore(services.conn)
    h.message(text='/start')
    h.press_text(texts.MENU_NEW_POST)
    return h


def bodies(h):
    return [h.session.visible[mid].get('text', '')
            for mid in sorted(h.session.alive('preview'))]


def test_each_text_previews_complete_signed_body_before_continue(services):
    h = live_harness(services)
    old = set()
    for i, text in enumerate(('first', 'second', 'third'), 1):
        h.message(message_id=40+i, text=text)
        current = h.session.alive('preview')
        assert len(current) == 1
        assert current.isdisjoint(old)
        assert not old.intersection(h.session.visible)
        assert bodies(h) == ['\n\n'.join(('first', 'second', 'third')[:i])
                             + '\n\nmain | partner']
        assert h.card_id() > max(current)
        assert not services.conn.execute('SELECT * FROM posts').fetchall()
        old = current


def test_replayed_input_does_not_resend_or_append_preview(services):
    h = live_harness(services)
    h.message(message_id=41, text='first')
    current = h.session.alive('preview')
    h.message(message_id=41, text='first')
    assert h.session.alive('preview') == current
    assert h.flow._staged(h.flow._ui()).text == 'first'


def test_each_separate_media_update_replaces_entire_album(services):
    h = live_harness(services)
    h.message(message_id=40, text='body')
    for i in range(10):
        old = h.session.alive('preview')
        h.message(message_id=41+i, photo=[{
            'file_id': f'p{i}', 'file_unique_id': f'u{i}',
            'width': 100, 'height': 100}])
        assert len(h.session.alive('preview')) == i+1
        assert not old.intersection(h.session.visible)
    current = h.session.alive('preview')
    h.message(message_id=60, photo=[{
        'file_id': 'extra', 'file_unique_id': 'extra', 'width': 100, 'height': 100}])
    assert h.session.alive('preview') == current
    assert 60 in h.session.visible
    assert not h.flow.input_captured(f'{OWNER}:60')


def test_long_body_replaces_every_part_without_early_post_rows(services):
    h = live_harness(services)
    paragraphs = ['a' * 2500, 'b' * 2500, 'c' * 2500]
    for i, paragraph in enumerate(paragraphs):
        old = h.session.alive('preview')
        h.message(message_id=40+i, text=paragraph)
        assert len(bodies(h)) == i+1
        assert all(body.startswith(p) for p, body in zip(paragraphs, bodies(h)))
        assert not old.intersection(h.session.visible)
    assert not services.conn.execute('SELECT * FROM posts').fetchall()


def test_caption_and_following_text_preserve_astral_entity_offsets(services):
    h = live_harness(services)
    h.message(message_id=40, text='😀')
    h.message(message_id=41, photo=[{
        'file_id': 'photo', 'file_unique_id': 'photo', 'width': 100, 'height': 100}],
        caption='bold', caption_entities=[{'type': 'bold', 'offset': 0, 'length': 4}])
    h.message(message_id=42, text='last')
    current = h.session.alive('preview')
    payload = h.session.visible[next(iter(current))]
    assert payload['caption'] == '😀\n\nbold\n\nlast\n\nmain | partner'
    assert payload['caption_entities'][0]['offset'] == 4


class FailingApi(SizedVisibleApi):
    fail_at = None
    sends = 0

    async def make_request(self, bot, method, timeout=None):
        if method.__api_method__.startswith('send'):
            self.sends += 1
            if self.sends == self.fail_at:
                self.requests.append((method.__api_method__, {}))
                raise TelegramBadRequest(method=method, message='isolated failure')
        return await super().make_request(bot, method, timeout)


def fail_sends(h, offset):
    # Reuse the visible ledger and its sequence when switching failure policy.
    h.session.__class__ = FailingApi
    h.session.fail_at = h.session.sends + offset


@pytest.mark.parametrize('offset', [2, 3])
def test_partial_stack_or_control_failure_keeps_last_complete_then_retry(services, offset):
    h = live_harness(services)
    h.message(message_id=40, text='a' * 2500)
    old = h.session.alive('preview')
    fail_sends(h, offset)
    if offset == 3:
        with pytest.raises(TelegramBadRequest):
            h.message(message_id=41, text='b' * 2500)
    else:
        h.message(message_id=41, text='b' * 2500)
    assert old.issubset(h.session.visible)
    assert h.flow.input_captured(f'{OWNER}:41')
    data = h.flow._navigation.lifecycle._load()
    known = set(data['active'] + data['candidate']) | {int(mid) for mid in data['pending']}
    assert h.session.alive('preview').issubset(known)
    # Delivery replay does not implicitly retry a failed private stack.
    sends = h.session.sends
    h.message(message_id=41, text='b' * 2500)
    assert h.flow._staged(h.flow._ui()).text == 'a' * 2500 + '\n\n' + 'b' * 2500
    assert h.session.sends == sends + 1  # controls only
    assert old.issubset(h.session.visible)
    h.press_text('Повторить предпросмотр')
    assert len(h.session.alive('preview')) == 2
    assert not old.intersection(h.session.visible)


def test_blocked_planner_keeps_payload_source_and_last_valid_preview(services):
    h = live_harness(services)
    h.message(message_id=40, text='valid')
    old = h.session.alive('preview')
    h.message(message_id=41, text='x' * 4096)
    assert h.session.alive('preview') == old
    assert 41 in h.session.visible
    assert h.flow._staged(h.flow._ui()).text == 'valid\n\n' + 'x' * 4096
    assert 'абзац' in h.last_text().lower()
    h.message(message_id=41, text='x' * 4096)
    assert 41 in h.session.visible
    assert h.session.alive('preview') == old
    assert 'абзац' in h.last_text().lower()


def test_forward_original_until_consent_then_signed_ephemeral_preview(services):
    h = live_harness(services)
    h.message(message_id=40, text='source', forward_origin={
        'type': 'hidden_user', 'date': h.now, 'sender_user_name': 'source'})
    assert 40 in h.session.visible
    assert not h.session.alive('preview')
    h.press_text(h.flow._copy_btn_label())
    assert bodies(h) == ['source\n\nmain | partner']
    assert 40 not in h.session.visible
    assert not services.conn.execute('SELECT * FROM posts').fetchall()


def test_schedule_questions_keep_preview_and_cancel_cleans_it(services):
    h = live_harness(services)
    h.message(message_id=40, text='body')
    h.press_text(texts.BTN_CONTINUE)
    current = h.session.alive('preview')
    assert h.flow._ui().arg('schedule_picker')
    h.press_text(texts.BTN_DAY_SLOTS)
    assert h.session.alive('preview') == current
    h.message(text='/cancel')
    assert not h.session.alive('preview')


@pytest.mark.parametrize('size', [1025, 2048])
def test_forward_large_caption_keeps_original_until_explicit_copy(services, size):
    h = live_harness(services)
    caption = 'x' * size
    h.message(message_id=40, photo=[{
        'file_id': 'photo', 'file_unique_id': 'photo', 'width': 100, 'height': 100}],
        caption=caption, forward_origin={
            'type': 'hidden_user', 'date': h.now, 'sender_user_name': 'source'})
    assert 40 in h.session.visible
    assert not h.session.alive('preview')
    h.press_text(h.flow._copy_btn_label())
    payloads = [h.session.visible[mid] for mid in sorted(h.session.alive('preview'))]
    assert any(p.get('text', '').startswith(caption) for p in payloads)
    assert any(p.get('photo') == 'photo' for p in payloads)
    assert 40 not in h.session.visible
    assert not services.conn.execute('SELECT * FROM posts').fetchall()


def test_video_and_photo_share_prepared_album_with_signed_caption(services):
    h = live_harness(services)
    h.message(message_id=40, video={
        'file_id': 'video', 'file_unique_id': 'v', 'width': 100, 'height': 100,
        'duration': 2}, caption='clip')
    old = h.session.alive('preview')
    assert len(old) == 1
    assert h.session.visible[next(iter(old))]['caption'] == 'clip\n\nmain | partner'
    h.message(message_id=41, photo=[{
        'file_id': 'photo', 'file_unique_id': 'p', 'width': 100, 'height': 100}])
    current = h.session.alive('preview')
    assert len(current) == 2
    assert not old.intersection(h.session.visible)
    payload = h.session.visible[next(iter(current))]
    assert [item['type'] for item in payload['media']] == ['video', 'photo']
    assert payload['media'][0]['caption'] == 'clip\n\nmain | partner'


def test_lost_intake_cas_does_not_send_preview_or_delete_source(services, monkeypatch):
    from poster_bot.ui.state import StaleStateError

    h = live_harness(services)
    h.message(message_id=40, text='first')
    old = h.session.alive('preview')

    def lost(*args, **kwargs):
        raise StaleStateError('isolated lost compare-and-set')

    monkeypatch.setattr(h.flow._state, 'set', lost)
    h.message(message_id=41, text='lost')
    assert h.session.alive('preview') == old
    assert 41 in h.session.visible
    assert not h.flow.input_captured(f'{OWNER}:41')
    assert h.flow._staged(h.flow._ui()).text == 'first'


def test_staged_retry_token_cannot_render_newer_intake(services):
    from test_ui_handlers_offline import _raw_message

    h = live_harness(services)
    fail_sends(h, 1)
    h.message(message_id=40, text='first')
    h.message(message_id=40, text='first')
    token = h._button_data('Повторить предпросмотр')
    h.message(message_id=41, text='second')
    before = len(h.session.requests)
    h.feed({'callback_query': {
        'id': 'stale-retry', 'from': {'id': OWNER, 'is_bot': False, 'first_name': 'x'},
        'chat_instance': 'ci', 'data': token,
        'message': _raw_message(message_id=h.card_id(), text='controls')}})
    assert [name for name, _ in h.session.requests[before:]] == ['answerCallbackQuery']


def test_nonowner_input_cannot_render_or_delete(services):
    h = live_harness(services)
    h.message(message_id=40, text='first')
    before = len(h.session.requests)
    h.message(message_id=41, text='foreign', **{
        'from': {'id': 999, 'is_bot': False, 'first_name': 'foreign'}})
    assert len(h.session.requests) == before
    assert 41 in h.session.visible
    assert h.flow._staged(h.flow._ui()).text == 'first'
