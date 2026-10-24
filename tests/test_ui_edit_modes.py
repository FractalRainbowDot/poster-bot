"""Whole-component edits through the real offline owner Dispatcher."""
import pytest

from poster_bot.domain.content import Entity, MediaRef
from poster_bot.domain.planner import LogicalPost, plan_post
from poster_bot.ui import texts
from test_ui_live_preview import live_harness
from test_ui_handlers_offline import OWNER


MODES = ('Отредактировать текст', 'Отредактировать вложение', 'Отредактировать всё')


def editing(services, media=None):
    h = live_harness(services)
    logical = LogicalPost(body='😀 original\n\ncaption',
                          entities=(Entity(type='bold', offset=3, length=8),),
                          media=media if media is not None else (
                              MediaRef(kind='photo', file_id='old-photo'),
                              MediaRef(kind='video', file_id='old-video')))
    plan = plan_post(logical, h.flow._signature).plan
    post_id = services.drafts.create_with_plan(logical, plan, now_epoch=h.now)
    h.press_text(texts.MENU_DRAFTS)
    # The list's selection label includes the post body; use its first button.
    h.press_text(labels(h)[0])
    h.press_text(texts.BTN_EDIT_CONTENT)
    return h, post_id, logical


def stored(h, post_id):
    return h.flow._plans.get(post_id).logical


def labels(h):
    payload = h.session.visible[h.card_id()]
    return [b['text'] for row in payload.get('reply_markup', {}).get('inline_keyboard', [])
            for b in row]


def photo(h, mid=51, caption=None):
    h.message(message_id=mid, photo=[{'file_id': 'new-photo', 'file_unique_id': 'new',
                                    'width': 100, 'height': 100}], caption=caption)


def test_mode_menu_and_text_replaces_entire_body_preserving_album(services):
    h, post_id, original = editing(services)
    assert [label for label in labels(h) if label in MODES] == list(MODES)
    h.press_text(MODES[0])
    h.message(message_id=50, text='new 😀', entities=[{'type': 'bold', 'offset': 0, 'length': 3}])
    assert stored(h, post_id) == original
    assert len(h.session.alive('preview')) == 2
    h.press_text(texts.BTN_CONTINUE)
    assert stored(h, post_id) == original  # Continue is NOT edit confirmation
    h.press_text(texts.BTN_CONFIRM)
    result = stored(h, post_id)
    assert result.body == 'new 😀'
    assert result.entities == (Entity(type='bold', offset=0, length=3),)
    assert result.media == original.media


def test_attachment_preserves_whole_text_and_entities_and_replaces_all_media(services):
    h, post_id, original = editing(services)
    h.press_text(MODES[1])
    photo(h)
    assert stored(h, post_id) == original
    preview = h.session.visible[next(iter(h.session.alive('preview')))]
    assert preview['caption'].startswith(original.body)
    h.press_text(texts.BTN_CONTINUE)
    h.press_text(texts.BTN_CONFIRM)
    result = stored(h, post_id)
    assert (result.body, result.entities) == (original.body, original.entities)
    assert result.media == (MediaRef(kind='photo', file_id='new-photo'),)


@pytest.mark.parametrize('mode', MODES)
def test_cancel_keeps_original_and_all_mode_starts_empty(services, mode):
    h, post_id, original = editing(services)
    h.press_text(mode)
    if mode == MODES[1]:
        photo(h)
    else:
        h.message(message_id=50, text='replacement')
    h.press_text(texts.BTN_CONTINUE)
    h.press_text(texts.BTN_CANCEL)
    assert stored(h, post_id) == original


@pytest.mark.parametrize('mode', MODES[:2])
def test_wrong_components_remain_visible_uncaptured(services, mode):
    h, post_id, original = editing(services)
    h.press_text(mode)
    version = h.flow._ui().version
    if mode == MODES[0]:
        photo(h, caption='must not disappear')
        mid = 51
    else:
        h.message(message_id=50, text='must not disappear')
        mid = 50
    assert not h.flow.input_captured(f'{OWNER}:{mid}')
    assert mid in h.session.visible
    assert h.flow._ui().version == version
    assert stored(h, post_id) == original


def test_attachment_caption_is_not_silently_dropped(services):
    h, post_id, original = editing(services)
    h.press_text(MODES[1])
    photo(h, caption='unexpected new text')
    assert 51 in h.session.visible
    assert not h.flow.input_captured(f'{OWNER}:51')
    assert stored(h, post_id) == original


@pytest.mark.parametrize('mode', MODES[:2])
@pytest.mark.parametrize('unsigned', [False, True])
def test_wrong_component_forward_is_retained_without_capture_or_discard(services, mode, unsigned):
    h, post_id, original = unsigned_editing(services) if unsigned else editing(services)
    h.press_text(mode)
    before = h.flow._ui()
    preview = h.session.alive('preview')
    origin = {'type': 'hidden_user', 'date': h.now, 'sender_user_name': 'source'}
    if mode == MODES[0]:
        h.message(message_id=50, photo=[{'file_id': 'source-photo',
                  'file_unique_id': 'u', 'width': 100, 'height': 100}],
                  forward_origin=origin)
    else:
        h.message(message_id=50, text='source text', forward_origin=origin)
    assert 50 in h.session.visible
    assert not h.flow.input_captured(f'{OWNER}:50')
    assert not h.flow.input_discarded(f'{OWNER}:50')
    assert h.flow._ui() == before
    assert h.session.alive('preview') == preview
    assert (services.drafts.get_content(post_id) if unsigned else stored(h, post_id)) == original


def test_all_rebuilds_and_replay_does_not_duplicate(services):
    h, post_id, original = editing(services)
    h.press_text(MODES[2])
    h.message(message_id=50, text='replacement')
    current = h.session.alive('preview')
    h.message(message_id=50, text='replacement')
    assert h.session.alive('preview') == current
    h.press_text(texts.BTN_CONTINUE)
    assert stored(h, post_id) == original
    h.press_text(texts.BTN_CONFIRM)
    assert stored(h, post_id) == LogicalPost(body='replacement')


def test_text_preserves_audio(services):
    h, post_id, _ = editing(services, media=(MediaRef(kind='audio', file_id='old-audio'),))
    h.press_text(MODES[0])
    h.message(message_id=50, text='new audio caption')
    h.press_text(texts.BTN_CONTINUE)
    h.press_text(texts.BTN_CONFIRM)
    assert stored(h, post_id).media == (MediaRef(kind='audio', file_id='old-audio'),)


def test_scheduled_edit_keeps_identity_deadline_and_is_applied_only_after_confirmation(services):
    from poster_bot.domain.time import Instant
    h, post_id, original = editing(services)
    pub = services.publications.schedule(post_id, Instant(h.now + 3600), now_epoch=h.now)
    before = dict(services.conn.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone())
    h.press_text(texts.MENU_PLAN)
    row = next(label for label in labels(h) if label != texts.MENU_NEW_POST
               and 'original' in label)
    h.press_text(row)
    h.press_text(texts.BTN_EDIT_CONTENT)
    assert [label for label in labels(h) if label in MODES] == list(MODES)
    h.press_text(MODES[0])
    h.message(message_id=50, text='edited scheduled body')
    h.press_text(texts.BTN_CONTINUE)
    assert services.publications.get(pub.id) == pub
    assert stored(h, post_id) == original
    h.press_text(texts.BTN_CONFIRM)
    after = services.publications.get(pub.id)
    assert after.id == pub.id and after.post_id == post_id
    assert after.scheduled_epoch == pub.scheduled_epoch
    assert after.version == pub.version + 1
    assert after.status.value == 'suspended'
    post = dict(services.conn.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone())
    assert post['created_at'] == before['created_at']
    pair = h.flow._plans.get(post_id)
    assert pair.plan.reconstructed_body() == pair.logical.body == 'edited scheduled body'
    assert pair.logical.media == original.media


def test_mode_choice_is_owner_and_conversation_bound(services):
    h, post_id, original = editing(services)
    token = h._button_data(MODES[0])
    version = h.flow._ui().version
    h.press_text(MODES[0], user_id=OWNER + 1)
    assert h.flow._ui().version == version
    h.press_text(MODES[2])
    h.feed({'callback_query': {'id': 'old-mode', 'from': {
        'id': OWNER, 'is_bot': False, 'first_name': 'owner'},
        'chat_instance': 'ci', 'data': token}})
    assert h.flow._ui().arg('edit_mode') == 'all'
    assert stored(h, post_id) == original


def test_failed_intake_cas_retains_source_and_original(services, monkeypatch):
    from poster_bot.ui.state import StaleStateError
    h, post_id, original = editing(services)
    h.press_text(MODES[0])
    version = h.flow._ui().version
    real_set = h.flow._state.set
    def fail(*args, **kwargs):
        if kwargs.get('effect_key') == f'{OWNER}:50':
            raise StaleStateError('isolated lost CAS')
        return real_set(*args, **kwargs)
    monkeypatch.setattr(h.flow._state, 'set', fail)
    h.message(message_id=50, text='not captured')
    assert h.flow._ui().version == version
    assert not h.flow.input_captured(f'{OWNER}:50')
    assert 50 in h.session.visible
    assert stored(h, post_id) == original


def test_forward_text_requires_copy_consent_then_keeps_original_media(services):
    h, post_id, original = editing(services)
    h.press_text(MODES[0])
    h.message(message_id=50, text='source replacement', forward_origin={
        'type': 'hidden_user', 'date': h.now, 'sender_user_name': 'source'})
    assert 50 in h.session.visible
    assert stored(h, post_id) == original
    h.press_text(h.flow._copy_btn_label())
    assert 50 not in h.session.visible
    assert len(h.session.alive('preview')) == 2
    h.press_text(texts.BTN_CONTINUE)
    h.press_text(texts.BTN_CONFIRM)
    result = stored(h, post_id)
    assert result.body == 'source replacement' and result.media == original.media


def test_new_input_invalidates_confirmation_and_refreshes_full_candidate(services):
    h, post_id, original = editing(services)
    h.press_text(MODES[0])
    h.message(message_id=50, text='first')
    h.press_text(texts.BTN_CONTINUE)
    token = h._button_data(texts.BTN_CONFIRM)
    h.message(message_id=51, text='second')
    assert not h.flow._ui().arg('edit_confirm')
    h.feed({'callback_query': {'id': 'old-confirm', 'from': {
        'id': OWNER, 'is_bot': False, 'first_name': 'owner'},
        'chat_instance': 'ci', 'data': token}})
    assert stored(h, post_id) == original
    h.press_text(texts.BTN_CONTINUE)
    h.press_text(texts.BTN_CONFIRM)
    assert stored(h, post_id).body == 'first\n\nsecond'


def test_changed_original_cannot_be_overwritten_by_old_mode_choice(services):
    h, post_id, _ = editing(services)
    newer = LogicalPost(body='newer durable original')
    plan = plan_post(newer, h.flow._signature).plan
    h.flow._plans.save_with_content(post_id, newer, plan, plan.parts[0].content,
                                   now_epoch=h.now)
    version = h.flow._ui().version
    h.press_text(MODES[0])
    assert h.flow._ui().version == version
    assert stored(h, post_id) == newer


def test_failed_atomic_save_preserves_replacement_and_original(services, monkeypatch):
    from poster_bot.domain.errors import ConflictError
    h, post_id, original = editing(services)
    h.press_text(MODES[0])
    h.message(message_id=50, text='durable replacement')
    h.press_text(texts.BTN_CONTINUE)
    version = h.flow._ui().version
    preview = h.session.alive('preview')
    def fail(*args, **kwargs):
        raise ConflictError('isolated atomic save failure')
    monkeypatch.setattr(h.flow._plans, 'save_with_content', fail)
    h.press_text(texts.BTN_CONFIRM)
    assert h.flow._ui().version == version
    assert h.flow._staged(h.flow._ui()).text == 'durable replacement'
    assert h.session.alive('preview') == preview
    assert stored(h, post_id) == original


def test_legacy_unsigned_text_edit_preserves_audio_and_waits_for_confirmation(services):
    from poster_bot.domain.content import Content
    from test_ui_preview_lifecycle import sized_harness
    h = sized_harness(services)
    original = Content(kind='audio', media=MediaRef(kind='audio', file_id='audio', caption='old'))
    post_id = services.drafts.create(original, now_epoch=h.now)
    h.message(text='/start')
    h.press_text(texts.MENU_DRAFTS)
    h.press_text(labels(h)[0])
    h.press_text(texts.BTN_EDIT_CONTENT)
    h.press_text(MODES[0])
    h.message(message_id=50, text='new caption')
    assert len(h.session.alive('preview')) == 1
    h.press_text(texts.BTN_CONTINUE)
    assert services.drafts.get_content(post_id) == original
    h.press_text(texts.BTN_CONFIRM)
    assert services.drafts.get_content(post_id).media == MediaRef(
        kind='audio', file_id='audio', caption='new caption')


def unsigned_editing(services):
    from poster_bot.domain.content import Content
    from test_ui_preview_lifecycle import sized_harness
    h = sized_harness(services)
    original = Content(kind='audio', media=MediaRef(
        kind='audio', file_id='old-audio', caption='😀 original',
        caption_entities=(Entity(type='bold', offset=3, length=8),)))
    post_id = services.drafts.create(original, now_epoch=h.now)
    h.message(text='/start')
    h.press_text(texts.MENU_DRAFTS)
    h.press_text(labels(h)[0])
    h.press_text(texts.BTN_EDIT_CONTENT)
    return h, post_id, original


@pytest.mark.parametrize('unsigned', [False, True])
@pytest.mark.parametrize('stale', [False, True])
def test_pending_attachment_choice_refuses_text_without_mutation(services, unsigned, stale):
    from poster_bot.ui.state import State
    h, post_id, original = unsigned_editing(services) if unsigned else editing(services)
    h.press_text(MODES[1])
    h.message(message_id=50, audio={'file_id': 'new-audio',
              'file_unique_id': 'u', 'duration': 1})
    photo(h)
    assert h.flow._ui().state is State.AWAIT_REPLACE
    if stale:
        if unsigned:
            from poster_bot.domain.content import Content
            original = Content(kind='text', text='newer original')
            services.conn.execute('UPDATE posts SET content_json=? WHERE id=?',
                                  (original.to_json(), post_id))
            services.conn.commit()
        else:
            original = LogicalPost(body='newer original')
            plan = plan_post(original, h.flow._signature).plan
            h.flow._plans.save_with_content(post_id, original, plan, plan.parts[0].content,
                                           now_epoch=h.now)
    before = h.flow._ui()
    preview = h.session.alive('preview')
    before_card = h.session.visible[h.card_id()]['text']
    h.message(message_id=52, text='must not become a caption or replace media')
    assert h.flow._ui() == before
    assert 52 in h.session.visible
    assert not h.flow.input_captured(f'{OWNER}:52')
    assert not h.flow.input_discarded(f'{OWNER}:52')
    assert h.session.alive('preview') == preview
    assert (services.drafts.get_content(post_id) if unsigned else stored(h, post_id)) == original
    card = h.session.visible[h.card_id()]['text']
    assert card == before_card if stale else 'без нового текста и подписей' in card
    if not stale:
        assert texts.BTN_REPLACE in labels(h) and texts.BTN_KEEP in labels(h)


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('saved_source', [False, True])
def test_unsigned_forward_edit_requires_copy_and_separate_confirmation(services, mode, saved_source):
    from poster_bot.ui.actions import ActionStore
    from poster_bot.domain.content import Content, ForwardSource
    from test_ui_handlers_offline import _raw_message
    h, post_id, original = unsigned_editing(services)
    referenced = Content(kind='forward', mode='forward',
                         forward=ForwardSource(chat_id=OWNER, message_ids=(50,)))
    saved_id = (services.drafts.create(referenced, now_epoch=h.now)
                if saved_source else None)
    # A still-valid capability from the legacy unsigned UI cannot bypass the
    # current component mode or the owner's explicit copy decision.
    legacy = ActionStore(services.conn).create(h.flow.A_FORWARD_AS_IS, now_epoch=h.now)
    h.press_text(mode)
    origin = {'type': 'hidden_user', 'date': h.now, 'sender_user_name': 'source'}
    entity = {'type': 'bold', 'offset': 0, 'length': 2}
    if mode == MODES[0]:
        h.message(message_id=50, text='😀 replacement', entities=[entity], forward_origin=origin)
    else:
        h.message(message_id=50, photo=[{'file_id': 'new-photo', 'file_unique_id': 'u',
                  'width': 100, 'height': 100}], forward_origin=origin,
                  caption='😀 replacement' if mode == MODES[2] else None,
                  caption_entities=[entity] if mode == MODES[2] else None)
    before = h.flow._ui()
    h.feed({'callback_query': {'id': 'old-as-is', 'from': {
        'id': OWNER, 'is_bot': False, 'first_name': 'owner'}, 'chat_instance': 'ci',
        'data': legacy, 'message': _raw_message(message_id=h.card_id(), text='card')}})
    assert services.drafts.get_content(post_id) == original
    assert h.flow._ui() == before
    assert texts.BTN_FORWARD_AS_IS not in labels(h)
    assert 50 in h.session.visible
    h.press_text(texts.BTN_CONTINUE)
    assert services.drafts.get_content(post_id) == original
    assert h.flow._staged(h.flow._ui()).kind == 'forward'
    h.press_text(h.flow._copy_btn_label())
    assert (50 in h.session.visible) == saved_source
    h.press_text(texts.BTN_CONTINUE)
    assert services.drafts.get_content(post_id) == original
    h.press_text(texts.BTN_CONFIRM)
    result = services.drafts.get_content(post_id)
    if mode == MODES[0]:
        assert result.media == MediaRef(kind='audio', file_id='old-audio',
            caption='😀 replacement', caption_entities=(Entity(type='bold', offset=0, length=2),))
    else:
        assert result.media == MediaRef(kind='photo', file_id='new-photo',
            caption='😀 original' if mode == MODES[1] else '😀 replacement',
            caption_entities=(Entity(type='bold', offset=3 if mode == MODES[1] else 0,
                                     length=8 if mode == MODES[1] else 2),))
    assert len(services.drafts.list_drafts()) == 1 + int(saved_source)
    if saved_source:
        assert services.drafts.get_content(saved_id) == referenced


@pytest.mark.parametrize('unsigned', [False, True])
@pytest.mark.parametrize('captioned', [False, True])
def test_pending_attachment_choice_retains_wrong_component_forward(services, unsigned, captioned):
    from poster_bot.ui.state import State
    h, post_id, original = unsigned_editing(services) if unsigned else editing(services)
    h.press_text(MODES[1])
    h.message(message_id=50, audio={'file_id': 'new-audio',
              'file_unique_id': 'u', 'duration': 1})
    photo(h)
    assert h.flow._ui().state is State.AWAIT_REPLACE
    before = h.flow._ui()
    preview = h.session.alive('preview')
    origin = {'type': 'hidden_user', 'date': h.now, 'sender_user_name': 'source'}
    if captioned:
        h.message(message_id=52, photo=[{'file_id': 'source-photo',
                  'file_unique_id': 's', 'width': 100, 'height': 100}],
                  caption='wrong component', forward_origin=origin)
    else:
        h.message(message_id=52, text='wrong component', forward_origin=origin)
    assert 52 in h.session.visible
    assert not h.flow.input_captured(f'{OWNER}:52')
    assert not h.flow.input_discarded(f'{OWNER}:52')
    assert h.flow._ui() == before
    assert h.session.alive('preview') == preview
    assert (services.drafts.get_content(post_id) if unsigned else stored(h, post_id)) == original
    assert texts.BTN_REPLACE in labels(h) and texts.BTN_KEEP in labels(h)


def test_unsigned_creation_still_accepts_as_is_forward_without_releasing_source(services):
    from poster_bot.domain.content import Content, ForwardSource
    from test_ui_preview_lifecycle import sized_harness
    h = sized_harness(services)
    h.message(text='/start')
    h.press_text(texts.MENU_NEW_POST)
    h.message(message_id=50, text='source', forward_origin={
        'type': 'hidden_user', 'date': h.now, 'sender_user_name': 'source'})
    assert texts.BTN_FORWARD_AS_IS in labels(h)
    h.press_text(texts.BTN_FORWARD_AS_IS)
    post_id, = services.drafts.list_drafts()
    assert services.drafts.get_content(post_id) == Content(kind='forward', mode='forward',
        forward=ForwardSource(chat_id=OWNER, message_ids=(50,)))
    assert 50 in h.session.visible
