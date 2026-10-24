"""Replacement capabilities through the real, offline Dispatcher."""
import pytest
from aiogram.exceptions import TelegramBadRequest

from poster_bot.ui import texts
from poster_bot.ui.state import State
from test_ui_edit_modes import MODES, editing, photo, unsigned_editing
from test_ui_edit_review_approval import press_token
from test_ui_handlers_offline import OWNER
from test_ui_live_preview import fail_sends
from test_ui_preview_lifecycle import sized_harness


DECISIONS = (texts.BTN_REPLACE, texts.BTN_KEEP, texts.BTN_AS_CAPTION)


def caption_question(services):
    h = sized_harness(services)
    h.message(text='/start')
    h.press_text(texts.MENU_NEW_POST)
    photo(h)
    h.message(message_id=52, text='candidate')
    assert h.flow._ui().state is State.AWAIT_REPLACE
    return h


def test_fresh_caption_replace_chooses_text_not_photo(services):
    h = caption_question(services)
    assert h.flow._staged(h.flow._ui()).build().kind == 'photo'
    assert not services.conn.execute('SELECT 1 FROM posts').fetchone()
    h.press_text(texts.BTN_REPLACE)
    assert h.flow._ui().state is State.CREATING
    staged = h.flow._staged(h.flow._ui())
    assert staged.kind == 'text' and staged.text == 'candidate'
    h.message(message_id=52, text='candidate')
    assert h.flow._staged(h.flow._ui()) == staged
    h.press_text(texts.BTN_CONTINUE)
    row = services.conn.execute('SELECT id FROM posts').fetchone()
    content = services.drafts.get_content(row['id'])
    assert content.kind == 'text' and content.text == 'candidate'


@pytest.mark.parametrize('decision', DECISIONS)
def test_fresh_caption_decisions_and_replay(services, decision):
    h = caption_question(services)
    token = h._button_data(decision)
    h.press_text(decision)
    current = h.flow._ui()
    assert current.state is State.CREATING
    content = h.flow._staged(current).build()
    if decision == texts.BTN_REPLACE:
        assert content.kind == 'text' and content.text == 'candidate'
    else:
        assert content.kind == 'photo'
        assert content.media.caption == ('candidate' if decision == texts.BTN_AS_CAPTION else None)
    press_token(h, token, 'duplicate-fresh')
    assert h.flow._ui() == current
    h.message(message_id=52, text='candidate')
    assert h.flow._ui() == current


@pytest.mark.parametrize('decision', DECISIONS[:2])
def test_failed_candidate_controls_replay_offers_fresh_decision(services, decision):
    h = caption_question(services)
    fail_sends(h, 1)
    with pytest.raises(TelegramBadRequest):
        h.message(message_id=53, text='new')
    h.message(message_id=53, text='new')
    assert texts.BTN_AS_CAPTION not in [
        b['text'] for row in h.session.visible[h.card_id()]['reply_markup']['inline_keyboard']
        for b in row]
    h.press_text(decision)
    assert h.flow._ui().state is State.CREATING
    content = h.flow._staged(h.flow._ui()).build()
    if decision == texts.BTN_REPLACE:
        assert content.kind == 'text' and content.text == 'new'
    else:
        assert content.kind == 'photo' and content.media.caption is None
    current = h.flow._ui()
    h.message(message_id=53, text='new')
    assert h.flow._ui() == current


@pytest.mark.parametrize('unsigned', [False, True])
@pytest.mark.parametrize('decision', DECISIONS[:2])
@pytest.mark.parametrize('after_ack', [False, True])
def test_edit_decision_rechecks_target_snapshot(services, unsigned, decision, after_ack):
    from poster_bot.domain.time import Instant
    h, pid, _ = unsigned_editing(services) if unsigned else editing(services)
    h.press_text(MODES[2])
    h.message(message_id=50, audio={'file_id': 'a', 'file_unique_id': 'a', 'duration': 1})
    photo(h)
    assert h.flow._ui().state is State.AWAIT_REPLACE
    token = h._button_data(decision)
    before = h.flow._ui()

    def change_target():
        # Isolated fixture service simulates a concurrent owner write.
        services.publications.schedule(pid, Instant(h.now + 3600), now_epoch=h.now)

    if after_ack:
        h.session.before_answer = change_target
    else:
        change_target()
    press_token(h, token, 'target-race')
    assert h.flow._ui() == before
    assert services.publications.get_by_post(pid) is not None


@pytest.mark.parametrize('decision', DECISIONS)
@pytest.mark.parametrize('transition', ['keep', 'candidate', 'ack'])
def test_old_decision_cannot_mutate_new_conversation(services, decision, transition):
    h = caption_question(services)
    token = h._button_data(decision)
    card = h.card_id()
    if transition == 'ack':
        h.session.before_answer = lambda: h.flow._go(
            State.AWAIT_REPLACE, dict(h.flow._ui().payload,
                                     pending_caption={'text': 'new', 'entities': []}),
            now=h.now)
        expected = None
    else:
        fail_sends(h, 1)
        with pytest.raises(TelegramBadRequest):
            if transition == 'keep':
                h.press_text(texts.BTN_REPLACE if decision == texts.BTN_KEEP
                             else texts.BTN_KEEP)
            else:
                h.message(message_id=53, text='new')
        assert h.card_id() == card and card in h.session.visible
        expected = h.flow._ui()
    before_visible = dict(h.session.visible)
    before_preview = h.session.alive('preview')
    press_token(h, token, 'old-decision')
    current = h.flow._ui()
    if expected is not None:
        assert current == expected
    else:
        assert current.state is State.AWAIT_REPLACE
        assert current.arg('pending_caption')['text'] == 'new'
    assert h.session.visible == before_visible
    assert h.session.alive('preview') == before_preview
    assert not services.conn.execute('SELECT 1 FROM posts').fetchone()
    h.session.before_answer = None
    press_token(h, token, 'replayed-decision')
    assert h.flow._ui() == current
    assert h.flow.input_captured(f'{OWNER}:52')


def test_repeated_edit_replacement_returns_to_bound_edit_and_requires_confirmation(services):
    from poster_bot.domain.content import MediaRef
    from poster_bot.domain.planner import LogicalPost
    from test_ui_edit_modes import stored

    h, pid, original = editing(services)
    h.press_text(MODES[2])
    destination = dict(h.flow._ui().payload)
    h.message(message_id=50, audio={'file_id': 'a', 'file_unique_id': 'a', 'duration': 1})
    photo(h)
    assert h.flow._ui().state is State.AWAIT_REPLACE
    h.message(message_id=52, text='extra text')
    assert stored(h, pid) == original
    h.press_text(texts.BTN_REPLACE)
    current = h.flow._ui()
    assert current.state is State.EDITING
    for key in ('target', 'post_id', 'edit_mode', 'edit_original', 'edit_snapshot'):
        assert current.arg(key) == destination[key]
    h.press_text(texts.BTN_CONTINUE)
    assert h.flow._ui().arg('edit_confirm')
    assert stored(h, pid) == original
    h.press_text(texts.BTN_CONFIRM)
    assert stored(h, pid) == LogicalPost(body='extra text', media=(
        MediaRef(kind='photo', file_id='new-photo'),))


@pytest.mark.parametrize('unsigned', [False, True])
@pytest.mark.parametrize('scheduled', [False, True])
@pytest.mark.parametrize('decision', (*DECISIONS[:2], texts.BTN_CANCEL))
def test_repeated_pending_updates_preserve_destination_and_original(
        services, unsigned, scheduled, decision):
    from poster_bot.domain.time import Instant
    from test_ui_edit_modes import labels

    h, pid, _ = unsigned_editing(services) if unsigned else editing(services)
    pub = None
    if scheduled:
        pub = services.publications.schedule(pid, Instant(h.now + 3600), now_epoch=h.now)
        h.press_text(texts.MENU_PLAN)
        h.press_text(next(label for label in labels(h) if 'original' in label))
        h.press_text(texts.BTN_EDIT_CONTENT)
    h.press_text(MODES[2])
    destination = dict(h.flow._ui().payload)
    post_before = dict(services.conn.execute('SELECT * FROM posts WHERE id=?', (pid,)).fetchone())
    plan_before = h.flow._plans.get(pid) if not unsigned else None
    h.message(message_id=50, audio={'file_id': 'a', 'file_unique_id': 'a', 'duration': 1})
    photo(h)
    old_tokens = [h._button_data(label) for label in
                  (texts.BTN_REPLACE, texts.BTN_KEEP, texts.BTN_CANCEL)]
    h.message(message_id=52, text='first extra')
    h.message(message_id=53, text='second extra')
    pending = h.flow._ui()
    for token in old_tokens:
        press_token(h, token, 'old-pending-control')
        assert h.flow._ui() == pending
    for mid in (50, 51, 52, 53):
        assert h.flow.input_captured(f'{OWNER}:{mid}')
    # Delivery replay neither consumes the candidate nor appends text twice.
    h.message(message_id=53, text='second extra')
    assert h.flow._ui() == pending
    h.press_text(decision)
    if decision == texts.BTN_CANCEL:
        assert h.flow._ui().state is (State.PUBLICATION if scheduled else State.DRAFT)
        assert h.flow._ui().arg('pub_id' if scheduled else 'post_id') == (pub.id if pub else pid)
    else:
        current = h.flow._ui()
        assert current.state is State.EDITING
        for key in ('target', 'post_id', 'pub_id', 'expected_version',
                    'edit_mode', 'edit_original', 'edit_snapshot'):
            assert current.arg(key) == destination.get(key)
        assert not any(key in current.payload for key in (
            'prev_state', 'pending_replace', 'pending_caption', 'edit_confirm', 'edit_review_version'))
        h.press_text(texts.BTN_CONTINUE)
        assert h.flow._ui().arg('edit_confirm')
    assert dict(services.conn.execute('SELECT * FROM posts WHERE id=?', (pid,)).fetchone()) == post_before
    if not unsigned:
        assert h.flow._plans.get(pid) == plan_before
    if pub:
        assert services.publications.get(pub.id) == pub
    if decision == texts.BTN_CANCEL:
        return
    h.press_text(texts.BTN_CONFIRM)
    result = services.drafts.get_content(pid) if unsigned else h.flow._plans.get(pid).logical
    if decision == texts.BTN_KEEP:
        assert (result.kind if unsigned else result.media[0].kind) == 'audio'
    elif unsigned:
        assert result.kind == 'text' and result.text == 'second extra'
    else:
        assert result.body == 'first extra\n\nsecond extra'
        assert result.media[0].file_id == 'new-photo'
    if pub:
        updated = services.publications.get(pub.id)
        assert (updated.id, updated.post_id, updated.scheduled_epoch) == (pub.id, pid, pub.scheduled_epoch)
        assert updated.version == pub.version + 1


@pytest.mark.parametrize('unsigned', [False, True])
@pytest.mark.parametrize('scheduled', [False, True])
@pytest.mark.parametrize('decision', DECISIONS[:2])
def test_cancel_after_repeated_replacement_resolution_returns_original_card(
        services, unsigned, scheduled, decision):
    from poster_bot.domain.time import Instant
    from test_ui_edit_modes import labels

    h, pid, _ = unsigned_editing(services) if unsigned else editing(services)
    pub = None
    if scheduled:
        pub = services.publications.schedule(pid, Instant(h.now + 3600), now_epoch=h.now)
        h.press_text(texts.MENU_PLAN)
        h.press_text(next(label for label in labels(h) if 'original' in label))
        h.press_text(texts.BTN_EDIT_CONTENT)
    before = dict(services.conn.execute('SELECT * FROM posts WHERE id=?', (pid,)).fetchone())
    plan_before = h.flow._plans.get(pid) if not unsigned else None
    h.press_text(MODES[2])
    h.message(message_id=50, audio={'file_id': 'a', 'file_unique_id': 'a', 'duration': 1})
    photo(h)
    h.message(message_id=52, text='pending replacement')
    h.press_text(decision)
    h.press_text(texts.BTN_CONTINUE)
    h.press_text(texts.BTN_CANCEL)
    assert h.flow._ui().state is (State.PUBLICATION if scheduled else State.DRAFT)
    assert h.flow._ui().arg('pub_id' if scheduled else 'post_id') == (pub.id if pub else pid)
    assert texts.BTN_EDIT_CONTENT in labels(h)
    assert dict(services.conn.execute('SELECT * FROM posts WHERE id=?', (pid,)).fetchone()) == before
    if not unsigned:
        assert h.flow._plans.get(pid) == plan_before
    if pub:
        assert services.publications.get(pub.id) == pub


@pytest.mark.parametrize('unsigned', [False, True])
@pytest.mark.parametrize('decision', DECISIONS[:2])
@pytest.mark.parametrize('update_kind', ['text', 'media'])
def test_repeated_creation_prompts_restore_creating(services, unsigned, decision, update_kind):
    from test_ui_live_preview import live_harness

    h = sized_harness(services) if unsigned else live_harness(services)
    if unsigned:
        h.message(text='/start')
        h.press_text(texts.MENU_NEW_POST)
    h.message(message_id=50, audio={'file_id': 'a', 'file_unique_id': 'a', 'duration': 1})
    photo(h)
    token = h._button_data(decision)
    if update_kind == 'text':
        h.message(message_id=52, text='new candidate')
    else:
        h.message(message_id=52, audio={'file_id': 'new-audio', 'file_unique_id': 'new-a', 'duration': 1})
    pending = h.flow._ui()
    press_token(h, token, 'old-create-choice')
    assert h.flow._ui() == pending
    assert not services.conn.execute('SELECT 1 FROM posts').fetchone()
    h.press_text(decision)
    assert h.flow._ui().state is State.CREATING
    h.press_text(texts.BTN_CONTINUE)
    pid, = services.drafts.list_drafts()
    content = services.drafts.get_content(pid)
    if decision == texts.BTN_KEEP:
        assert content.kind == 'audio' and content.media.file_id == 'a'
    elif update_kind == 'media':
        assert content.kind == 'audio' and content.media.file_id == 'new-audio'
    elif unsigned:
        assert content.kind == 'text' and content.text == 'new candidate'
    else:
        logical = h.flow._plans.get(pid).logical
        assert logical.body == 'new candidate' and logical.media[0].file_id == 'new-photo'


@pytest.mark.parametrize('unsigned', [False, True])
@pytest.mark.parametrize('scheduled', [False, True])
@pytest.mark.parametrize('decision', (*DECISIONS[:2], texts.BTN_CANCEL))
def test_repeated_saved_raw_forward_recovery_keeps_destination(
        services, unsigned, scheduled, decision):
    from poster_bot.domain.content import Content, ForwardSource
    from poster_bot.domain.time import Instant
    from test_ui_edit_modes import labels
    from test_ui_live_preview import live_harness

    h = sized_harness(services) if unsigned else live_harness(services)
    original = Content(kind='forward', mode='forward',
                       forward=ForwardSource(chat_id=OWNER, message_ids=(90,)))
    pid = services.drafts.create(original, now_epoch=h.now)
    pub = services.publications.schedule(pid, Instant(h.now + 3600), now_epoch=h.now) if scheduled else None
    h.press_text(texts.MENU_PLAN if scheduled else texts.MENU_DRAFTS)
    h.press_text(next(label for label in labels(h) if 'перес' in label.lower() or label.startswith('✉')))
    h.press_text(texts.BTN_EDIT_CONTENT if unsigned else texts.BTN_FORWARD_RESEND)
    destination = dict(h.flow._ui().payload)
    h.message(message_id=50, audio={'file_id': 'a', 'file_unique_id': 'a', 'duration': 1})
    photo(h)
    h.message(message_id=52, audio={'file_id': 'new-audio', 'file_unique_id': 'new-a', 'duration': 1})
    h.press_text(decision)
    assert services.drafts.get_content(pid) == original
    assert h.flow.forward_source_referenced(chat_id=OWNER, message_id=90)
    if pub:
        assert services.publications.get(pub.id) == pub
    if decision == texts.BTN_CANCEL:
        assert h.flow._ui().state is (State.PUBLICATION if scheduled else State.DRAFT)
        assert h.flow._ui().arg('pub_id' if scheduled else 'post_id') == (pub.id if pub else pid)
        return
    assert h.flow._ui().state is State.EDITING
    for key in ('target', 'post_id', 'pub_id', 'expected_version', 'edit_mode', 'edit_snapshot'):
        assert h.flow._ui().arg(key) == destination.get(key)
    h.press_text(texts.BTN_CONTINUE)
    assert h.flow._ui().arg('edit_confirm')
    assert services.drafts.get_content(pid) == original
    h.press_text(texts.BTN_CONFIRM)
    result = services.drafts.get_content(pid)
    assert result.kind == 'audio'
    assert result.media.file_id == ('new-audio' if decision == texts.BTN_REPLACE else 'a')
    if pub:
        updated = services.publications.get(pub.id)
        assert (updated.id, updated.post_id, updated.scheduled_epoch) == (pub.id, pid, pub.scheduled_epoch)
        assert updated.version == pub.version + 1
