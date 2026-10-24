"""Automatic stored-card previews through the configured offline Dispatcher."""
import pytest

from poster_bot.domain.planner import plan_post_for_version
from poster_bot.domain.signature import SignatureConfig
from poster_bot.ui import texts
from poster_bot.ui.state import State
from test_ui_live_preview import live_harness, bodies, fail_sends
from test_ui_handlers_offline import OWNER, _raw_message
from conftest import text_content


def current_labels(h):
    payload = h.session.visible[h.card_id()]
    return [b['text'] for row in payload.get('reply_markup', {}).get('inline_keyboard', [])
            for b in row]


def open_row(h, post):
    h.press_text(next(label for label in current_labels(h)
                      if f'№{post}' in label or 'aaaa' in label))


def callback(h, token):
    h.feed({'callback_query': {
        'id': f'cb{h._update_id}',
        'from': {'id': OWNER, 'is_bot': False, 'first_name': 'owner'},
        'chat_instance': 'ci', 'data': token,
        'message': _raw_message(message_id=h.card_id(), text='card'),
    }})


def stored(h):
    h.message(message_id=40, text='a' * 2500)
    h.message(message_id=41, text='b' * 2500)
    h.press_text(texts.BTN_CONTINUE)
    return h.flow._ui().arg('post_id')


def assert_card(h):
    assert len(bodies(h)) == 2
    assert all('main | partner' in body for body in bodies(h))
    assert h.card_id() > max(h.session.alive('preview'))
    assert texts.BTN_PREVIEW not in current_labels(h)
    assert texts.BTN_PAUSE not in current_labels(h)


@pytest.mark.parametrize('path', ['draft_row', 'draft_cancel_edit', 'draft_back',
                                  'confirmation', 'scheduled', 'publication_row',
                                  'publication_cancel_edit', 'unschedule'])
def test_every_stored_card_path_has_full_signed_preview(services, path):
    h = live_harness(services)
    post = stored(h)
    if path.startswith('draft'):
        h.press_text(texts.MENU_DRAFTS)
        assert not h.session.alive('preview')
        open_row(h, post)
        if path == 'draft_cancel_edit':
            h.press_text(texts.BTN_EDIT_CONTENT)
            h.press_text(texts.BTN_CANCEL)
            h.press_text(texts.MENU_DRAFTS)
            open_row(h, post)
        elif path == 'draft_back':
            h.press_text(texts.BTN_SCHEDULE)
            h.press_text(texts.BTN_DAY_SLOTS)
            h.press_text(texts.BTN_BACK)
    else:
        h.message(message_id=42, text='2359')
        assert services.publications.get_by_post(post) is None
        if path != 'confirmation':
            h.press_text(texts.BTN_CONFIRM)
            pub = services.publications.get_by_post(post)
            assert pub is not None
            if path == 'publication_row':
                h.press_text(texts.MENU_PLAN)
                assert not h.session.alive('preview')
                open_row(h, post)
            elif path == 'publication_cancel_edit':
                h.press_text(texts.BTN_EDIT_CONTENT)
                h.press_text(texts.BTN_CANCEL)
                h.press_text(texts.MENU_PLAN)
                open_row(h, post)
            elif path == 'unschedule':
                h.press_text(texts.BTN_TO_DRAFTS)
                h.press_text(texts.BTN_YES)
                assert services.publications.get_by_post(post) is None
                assert services.drafts.is_draft(post)
                assert h.flow._plans.get(post) is not None
    assert_card(h)


def test_unchanged_context_and_update_replay_do_not_duplicate_stack(services):
    h = live_harness(services)
    stored(h)
    before = h.session.alive('preview')
    h.message(message_id=42, text='2359')
    assert h.session.alive('preview') == before
    h.message(message_id=42, text='2359')
    assert h.session.alive('preview') == before
    token = h._button_data(texts.BTN_CONFIRM)
    h.press_text(texts.BTN_CONFIRM)
    assert h.session.alive('preview') == before
    callback(h, token)
    assert h.session.alive('preview') == before


def test_partial_stored_preview_failure_keeps_old_then_retry_current_plan(services):
    h = live_harness(services)
    post = stored(h)
    old = h.session.alive('preview')
    # A new frozen version must replace the visible old version, but not until
    # ALL parts and the new control card have succeeded.
    saved = h.flow._plans.get(post)
    signature = SignatureConfig('new-main', 'https://example.com/new',
                                'partner', 'https://example.com/partner')
    plan = plan_post_for_version(saved.logical, signature, version=saved.plan.version).plan
    h.flow._plans.save_with_content(post, saved.logical, plan, plan.parts[0].content,
                                   now_epoch=h.now)
    fail_sends(h, 2)
    h.message(message_id=42, text='2359')
    assert h.flow._ui().state is State.MANUAL_TIME
    assert old.issubset(h.session.visible)
    assert services.publications.get_by_post(post) is None
    assert 'Повторить предпросмотр' in current_labels(h)
    h.press_text('Повторить предпросмотр')
    assert h.flow._ui().state is State.CONFIRM_SCHEDULE
    assert len(bodies(h)) == 2
    assert all('new-main | partner' in body for body in bodies(h))
    assert not old.intersection(h.session.visible)


def test_failed_detail_retry_is_version_bound_and_does_not_repeat_input(services):
    h = live_harness(services)
    post = stored(h)
    h.press_text(texts.MENU_DRAFTS)
    fail_sends(h, 1)
    open_row(h, post)
    retry = h._button_data('Повторить предпросмотр')
    assert not h.session.alive('preview')
    h.press_text('Повторить предпросмотр')
    assert_card(h)
    current = h.session.alive('preview')
    callback(h, retry)
    assert h.session.alive('preview') == current
    assert len(services.conn.execute('SELECT * FROM posts').fetchall()) == 1


def test_automatic_review_uses_frozen_v2_not_ambient_signature(services):
    h = live_harness(services)
    post = stored(h)
    saved = h.flow._plans.get(post)
    legacy = plan_post_for_version(saved.logical, saved.plan.signature, version=2).plan
    h.flow._plans.save_with_content(post, saved.logical, legacy,
                                   legacy.parts[0].content, now_epoch=h.now)
    frozen = h.flow._plans.get(post).plan.to_json()
    h.flow._signature = SignatureConfig('ambient', 'https://example.com/ambient',
                                        'other', 'https://example.com/other')
    h.press_text(texts.MENU_DRAFTS)
    open_row(h, post)
    assert bodies(h) == [part.content.text for part in legacy.parts]
    assert h.flow._plans.get(post).plan.to_json() == frozen


def test_missing_frozen_plan_never_gets_synthesized_by_automatic_review(services):
    h = live_harness(services)
    post = services.drafts.create(text_content(), now_epoch=h.now)
    h.press_text(texts.MENU_DRAFTS)
    open_row(h, post)
    assert not h.session.alive('preview')
    assert h.flow._plans.get(post) is None
    assert 'Повторить предпросмотр' in current_labels(h)
    assert texts.BTN_EDIT_CONTENT in current_labels(h)


def test_main_plan_inline_create_uses_existing_flow_and_stale_token_is_harmless(services):
    h = live_harness(services)
    post = stored(h)
    h.press_text(texts.MENU_PLAN)
    assert 'Создать пост' in current_labels(h)
    assert texts.BTN_BACK not in current_labels(h)
    token = h._button_data('Создать пост')
    callback(h, token)
    assert h.flow._ui().state is State.CREATING
    assert h.flow._staged(h.flow._ui()).empty
    assert services.drafts.is_draft(post)
    assert not h.session.alive('preview')
    h.message(message_id=45, text='new body')
    before = h.session.alive('preview')
    ui = h.flow._ui()
    callback(h, token)
    assert h.flow._ui() == ui
    assert h.session.alive('preview') == before
    assert services.drafts.is_draft(post)


def test_unconsumed_main_create_token_is_stale_after_leaving_dashboard(services):
    h = live_harness(services)
    h.press_text(texts.MENU_PLAN)
    token = h._button_data('Создать пост')
    h.press_text(texts.MENU_NEW_POST)
    h.message(message_id=40, text='keep current intake')
    before, ui = h.session.alive('preview'), h.flow._ui()
    callback(h, token)
    assert h.flow._ui() == ui
    assert h.session.alive('preview') == before


def test_stored_album_tracks_every_ack_and_auto_reopens_full_group(services):
    h = live_harness(services)
    for i in range(10):
        h.message(message_id=40+i, photo=[{
            'file_id': f'p{i}', 'file_unique_id': f'u{i}', 'width': 100, 'height': 100}])
    before = h.session.alive('preview')
    h.press_text(texts.BTN_CONTINUE)
    post = h.flow._ui().arg('post_id')
    assert h.session.alive('preview') == before
    h.press_text(texts.MENU_DRAFTS)
    open_row(h, post)
    current = h.session.alive('preview')
    assert len(current) == 10
    assert set(h.flow._navigation.lifecycle._load()['active']) == current
    assert current.isdisjoint(before)
    assert texts.BTN_PREVIEW not in current_labels(h)


def test_stored_control_failure_replay_requires_explicit_retry_before_approval(services):
    from aiogram.exceptions import TelegramBadRequest

    h = live_harness(services)
    post = stored(h)
    old = h.session.alive('preview')
    saved = h.flow._plans.get(post)
    signature = SignatureConfig('updated', 'https://example.com/new',
                                'partner', 'https://example.com/partner')
    plan = plan_post_for_version(saved.logical, signature, version=saved.plan.version).plan
    h.flow._plans.save_with_content(post, saved.logical, plan, plan.parts[0].content,
                                   now_epoch=h.now)
    fail_sends(h, 3)
    with pytest.raises(TelegramBadRequest):
        h.message(message_id=42, text='2359')
    assert old.issubset(h.session.visible)
    sends = h.session.sends
    h.message(message_id=42, text='2359')
    assert h.session.sends == sends + 1  # replay only sends controls
    assert texts.BTN_CONFIRM not in current_labels(h)
    assert 'Повторить предпросмотр' in current_labels(h)
    h.press_text('Повторить предпросмотр')
    assert h.flow._ui().state is State.CONFIRM_SCHEDULE
    assert all('updated | partner' in body for body in bodies(h))
    assert not old.intersection(h.session.visible)
    assert services.publications.get_by_post(post) is None


def test_main_inline_create_is_owner_only_without_consuming_owner_capability(services):
    h = live_harness(services)
    h.press_text(texts.MENU_PLAN)
    token = h._button_data('Создать пост')
    ui = h.flow._ui()
    h.feed({'callback_query': {
        'id': 'foreign', 'from': {'id': 999, 'is_bot': False, 'first_name': 'foreign'},
        'chat_instance': 'ci', 'data': token,
        'message': _raw_message(message_id=h.card_id(), text='card'),
    }})
    assert h.flow._ui() == ui
    callback(h, token)
    assert h.flow._ui().state is State.CREATING


def test_old_confirmation_cannot_schedule_a_missing_frozen_plan(services):
    from conftest import future
    from ui_flow_helpers import run

    h = live_harness(services)
    post = services.drafts.create(text_content(), now_epoch=h.now)
    # Simulate a durable confirmation left by an older UI, before rendering.
    reply = h.flow._confirm_schedule_view(now=h.now, post_id=post,
                                         instant=future(minutes=60))
    run(h.flow.press(now=h.now, token=reply.rows[0][0].token))
    assert services.publications.get_by_post(post) is None
    assert h.flow._plans.get(post) is None
