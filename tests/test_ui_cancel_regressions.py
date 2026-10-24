"""Freshly rendered cancel buttons through the real offline Dispatcher."""
import pytest

from poster_bot.ui import texts
from poster_bot.domain.time import Instant
from poster_bot.ui.state import State
from test_ui_edit_modes import MODES, editing, labels, photo, stored, unsigned_editing
from test_ui_edit_review_approval import press_token
from test_ui_handlers_offline import OWNER
from test_ui_live_preview import fail_sends, live_harness


@pytest.mark.parametrize('stage', ['picker', 'empty', 'collected', 'confirmation'])
@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('scheduled', [False, True])
@pytest.mark.parametrize('unsigned', [False, True])
def test_fresh_edit_cancel_returns_original_card(services, mode, stage, scheduled, unsigned):
    h, post_id, original = unsigned_editing(services) if unsigned else editing(services)
    original_plan = None if unsigned else h.flow._plans.get(post_id)
    if scheduled:
        pub = services.publications.schedule(post_id, Instant(h.now + 3600), now_epoch=h.now)
        h.press_text(texts.MENU_PLAN)
        h.press_text(next(label for label in labels(h) if 'original' in label))
        h.press_text(texts.BTN_EDIT_CONTENT)
        pub = services.publications.get(pub.id)
    before = dict(services.conn.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone())
    if stage != 'picker':
        h.press_text(mode)
    if stage in ('collected', 'confirmation'):
        if mode == MODES[1]:
            photo(h)
        else:
            h.message(message_id=50, text='replacement')
    if stage == 'confirmation':
        h.press_text(texts.BTN_CONTINUE)
    h.press_text(texts.BTN_CANCEL)
    assert texts.BTN_EDIT_CONTENT in labels(h)
    assert (services.drafts.get_content(post_id) if unsigned else stored(h, post_id)) == original
    assert dict(services.conn.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()) == before
    if not unsigned:
        assert h.flow._plans.get(post_id) == original_plan
    if scheduled:
        assert services.publications.get(pub.id) == pub
    assert h.session.alive('preview')


@pytest.mark.parametrize('unsigned', [False, True])
@pytest.mark.parametrize('mode', MODES[1:])
def test_pending_replace_cancel_returns_original(services, unsigned, mode):
    h, pid, original = unsigned_editing(services) if unsigned else editing(services)
    h.press_text(mode)
    h.message(message_id=50, audio={'file_id': 'a', 'file_unique_id': 'a', 'duration': 1})
    photo(h)
    assert h.flow._ui().state is State.AWAIT_REPLACE
    h.press_text(texts.BTN_CANCEL)
    assert texts.BTN_EDIT_CONTENT in labels(h)
    assert (services.drafts.get_content(pid) if unsigned else stored(h, pid)) == original


@pytest.mark.parametrize('saved_source', [False, True])
@pytest.mark.parametrize('mode', MODES)
def test_forward_consent_cancel_releases_only_unreferenced_source(services, saved_source, mode):
    from poster_bot.domain.content import Content, ForwardSource
    h, pid, original = editing(services)
    if saved_source:
        source = Content(kind='forward', mode='forward',
                         forward=ForwardSource(chat_id=OWNER, message_ids=(50,)))
        saved = services.drafts.create(source, now_epoch=h.now)
    h.press_text(mode)
    origin = {'type': 'hidden_user', 'date': h.now, 'sender_user_name': 'source'}
    if mode == MODES[0]:
        h.message(message_id=50, text='source replacement', forward_origin=origin)
    else:
        h.message(message_id=50, photo=[{'file_id': 'source-photo', 'file_unique_id': 's',
                  'width': 100, 'height': 100}], forward_origin=origin)
    assert 50 in h.session.visible
    assert h.flow.input_captured(f'{OWNER}:50')
    h.press_text(texts.BTN_CANCEL)
    assert texts.BTN_EDIT_CONTENT in labels(h)
    assert stored(h, pid) == original
    assert (50 in h.session.visible) == saved_source
    h.message(message_id=50, text='source replacement', forward_origin=origin)
    assert h.flow._ui().state is State.DRAFT
    assert stored(h, pid) == original
    if saved_source:
        assert services.drafts.get_content(saved) == source


@pytest.mark.parametrize('after_retry', [False, True])
def test_failed_edit_preview_cancel(services, after_retry):
    h, pid, original = editing(services)
    h.press_text(MODES[0])
    fail_sends(h, 1)
    h.message(message_id=50, text='replacement')
    assert 'Повторить предпросмотр' in labels(h)
    if after_retry:
        h.press_text('Повторить предпросмотр')
    h.press_text(texts.BTN_CANCEL)
    assert texts.BTN_EDIT_CONTENT in labels(h)
    assert stored(h, pid) == original
    assert len(h.session.alive('preview')) == 2


@pytest.mark.parametrize('race', [False, True])
def test_cancel_capability_cannot_leave_newer_edit(services, race):
    h, pid, original = editing(services)
    token = h._button_data(texts.BTN_CANCEL)
    if race:
        h.session.before_answer = lambda: h.flow._go(
            State.EDITING, dict(h.flow._ui().payload, edit_mode='all'), now=h.now)
    else:
        h.press_text(MODES[2])
    press_token(h, token, 'cancel-old')
    assert h.flow._ui().state is State.EDITING
    assert h.flow._ui().arg('edit_mode') == 'all'
    assert stored(h, pid) == original


def test_create_cancel_and_old_button_cannot_cancel_new_collection(services):
    h = live_harness(services)
    token = h._button_data(texts.BTN_CANCEL)
    h.message(message_id=50, text='first')
    before = h.flow._ui()
    press_token(h, token, 'old-create-cancel')
    assert h.flow._ui() == before
    h.press_text(texts.BTN_CANCEL)
    assert h.flow._ui().state is State.MENU
    assert not h.session.alive('preview')
    assert not services.conn.execute('SELECT 1 FROM posts').fetchone()


def test_legacy_caption_question_offers_working_cancel_including_replay(services):
    from test_ui_preview_lifecycle import sized_harness
    h = sized_harness(services)
    h.message(text='/start')
    h.press_text(texts.MENU_NEW_POST)
    photo(h)
    h.message(message_id=52, text='caption candidate')
    assert h.flow._ui().state is State.AWAIT_REPLACE
    assert h.flow._ui().arg('pending_caption')
    assert texts.BTN_CANCEL in labels(h)
    h.message(message_id=52, text='caption candidate')
    assert texts.BTN_CANCEL in labels(h)
    h.press_text(texts.BTN_CANCEL)
    assert h.flow._ui().state is State.MENU
    assert not services.conn.execute('SELECT 1 FROM posts').fetchone()
