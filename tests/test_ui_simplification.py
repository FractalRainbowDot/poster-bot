"""Explicit UI simplifications; no assumptions about unavailable screenshots."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import NOW, text_content, future
from poster_bot.domain.time import Instant
from poster_bot.journal import ALLOWED_KINDS
from poster_bot.ui import texts
from ui_flow_helpers import build_flow, labels, press_label
from ui_flow_helpers import run
from poster_bot.ui.state import State


def test_home_has_day_navigation_but_no_back(services):
    flow, _ = build_flow(services)
    reply = flow.plan_today(now=NOW)
    assert texts.BTN_BACK not in labels(reply)
    assert {'← Пред.', 'Сегодня', 'След. →'} <= set(labels(reply))
    assert 'Создать пост' in labels(reply)
    assert all(reply.rows)


def test_journal_twenty_entries_per_page_and_no_missing_entries(services):
    flow, _ = build_flow(services)
    for i in range(45):
        services.journal.record('draft_created', post_id=i + 1, now_epoch=NOW + i)
    first = flow.journal_view(now=NOW, page=0)
    assert '(1–20 из 45)' in first.text
    assert len(first.text.splitlines()) == 21
    second = press_label(flow, first, 'След. →', now=NOW)
    assert '(21–40 из 45)' in second.text
    third = press_label(flow, second, 'След. →', now=NOW)
    assert '(41–45 из 45)' in third.text
    assert len(third.text.splitlines()) == 6
    assert 'След. →' not in labels(third)


@pytest.mark.parametrize('kind,detail,marker', [
    ('draft_created', {}, 'ℹ️'), ('published', {}, '✅'),
    ('marked_uncertain', {}, '⚠️'), ('marked_missed', {}, '⚠️'),
    ('send_failed_definitive', {}, '❌'),
    ('attempt_finished', {'outcome': 'definitive_failure'}, '❌'),
    ('attempt_finished', {'outcome': 'published'}, '✅'),
])
def test_journal_severity_uses_event_kind_and_result(services, kind, detail, marker):
    flow, _ = build_flow(services)
    services.journal.record(kind, now_epoch=NOW, **detail)
    reply = flow.journal_view(now=NOW, page=0)
    assert reply.text.splitlines()[1].startswith(marker + ' ')


def test_every_actual_journal_event_has_a_severity(services):
    from poster_bot.ui.journal_presentation import EVENT_MARKERS
    assert set(EVENT_MARKERS) == set(ALLOWED_KINDS)


@pytest.mark.parametrize('timezone,expected', [
    ('Europe/Moscow', '12:34 22.10.2026'),
    ('Pacific/Honolulu', '23:34 21.10.2026'),
])
def test_exact_final_confirmation_uses_configured_local_date(services, timezone, expected):
    flow, _ = build_flow(services)
    services.settings.change(timezone=timezone)
    post = services.drafts.create(text_content(), now_epoch=NOW)
    epoch = int(datetime(2026, 10, 22, 9, 34, tzinfo=ZoneInfo('UTC')).timestamp())
    reply = flow._confirm_schedule_view(now=NOW, post_id=post, instant=Instant(epoch))
    assert reply.text == f'❗ Публикация будет запланирована на {expected} ❗'
    assert reply.review_post_id == post
    assert texts.BTN_CONFIRM in labels(reply)
    assert services.publications.get_by_post(post) is None


@pytest.mark.parametrize('clock', ['2359', '23:59', '23-59', '23.59', '23 59'])
def test_picker_manual_time_goes_straight_to_final_confirmation(services, clock):
    flow, _ = build_flow(services)
    post = services.drafts.create(text_content(), now_epoch=NOW)
    picker = flow.draft_schedule_ask(now=NOW, post_id=post)
    assert texts.BTN_OWN_TIME not in labels(picker)
    reply = run(flow.text(now=NOW, text=clock, source_key='time:1'))
    assert flow._ui().state is State.CONFIRM_SCHEDULE
    assert texts.BTN_CONFIRM in labels(reply)
    assert services.publications.get_by_post(post) is None


def test_empty_and_invalid_time_form_remains_correctable_without_continue(services):
    flow, _ = build_flow(services)
    post = services.drafts.create(text_content(), now_epoch=NOW)
    reply = flow._start_time_form(now=NOW, payload={'purpose': 'schedule', 'post_id': post})
    assert labels(reply) == ['Изменить дату', texts.BTN_BACK]
    reply = run(flow.text(now=NOW, text='0000', source_key='time:1'))
    assert flow._ui().state is State.MANUAL_TIME
    assert texts.BTN_CONTINUE not in labels(reply)
    reply = press_label(flow, reply, 'Изменить дату', now=NOW)
    reply = press_label(flow, reply, 'Завтра', now=NOW)
    assert flow._ui().state is State.CONFIRM_SCHEDULE
    assert texts.BTN_CONFIRM in labels(reply)
    assert services.publications.get_by_post(post) is None


def test_date_prompt_still_accepts_bare_time(services):
    flow, _ = build_flow(services)
    post = services.drafts.create(text_content(), now_epoch=NOW)
    reply = flow._start_time_form(now=NOW, payload={'purpose': 'schedule', 'post_id': post})
    press_label(flow, reply, 'Изменить дату', now=NOW)
    reply = run(flow.text(now=NOW, text='2359', source_key='time:1'))
    assert flow._ui().state is State.CONFIRM_SCHEDULE
    assert texts.BTN_CONFIRM in labels(reply)


def test_final_confirmation_can_change_date_time_and_reject_bad_input(services):
    flow, _ = build_flow(services)
    post = services.drafts.create(text_content(), now_epoch=NOW)
    flow.draft_schedule_ask(now=NOW, post_id=post)
    reply = run(flow.text(now=NOW, text='2359', source_key='time:1'))
    before = flow._ui()
    bad = run(flow.text(now=NOW, text='hello', source_key='time:2'))
    assert texts.TIME_BAD_INPUT in bad.text
    assert flow._ui() == before and not flow.input_captured('time:2')
    reply = press_label(flow, reply, 'Изменить дату', now=NOW)
    reply = press_label(flow, reply, 'Завтра', now=NOW)
    assert flow._ui().state is State.CONFIRM_SCHEDULE
    reply = run(flow.text(now=NOW, text='2310', source_key='time:3'))
    assert '23:10' in reply.text
    assert flow.input_captured('time:3')
    assert services.publications.get_by_post(post) is None


def test_day_slots_accept_manual_clock_on_selected_day(services):
    flow, _ = build_flow(services)
    post = services.drafts.create(text_content(), now_epoch=NOW)
    reply = flow.draft_schedule_ask(now=NOW, post_id=post)
    reply = press_label(flow, reply, texts.BTN_DAY_SLOTS, now=NOW)
    press_label(flow, reply, 'День →', now=NOW)
    reply = run(flow.text(now=NOW, text='810', source_key='time:1'))
    assert '08:10 22.10.2026' in reply.text
    assert flow._ui().state is State.CONFIRM_SCHEDULE


def test_dispatcher_creation_skips_saved_and_completed_forms_without_early_schedule(services):
    from test_ui_live_preview import live_harness
    h = live_harness(services)
    h.message(text='first', message_id=40)
    h.message(text='second', message_id=41)
    h.message(message_id=42, photo=[{
        'file_id': 'p', 'file_unique_id': 'u', 'width': 100, 'height': 100}])
    assert not services.conn.execute('SELECT * FROM posts').fetchall()
    h.press_text(texts.BTN_CONTINUE)
    ui = h.flow._ui()
    assert ui.state is State.DRAFT and ui.arg('schedule_picker')
    post = ui.arg('post_id')
    assert len(services.conn.execute('SELECT * FROM posts').fetchall()) == 1
    assert h.session.alive('preview')
    h.message(text='2359', message_id=43)
    assert h.flow._ui().state is State.CONFIRM_SCHEDULE
    assert services.publications.get_by_post(post) is None
    current = h.session.alive('preview')
    assert h.card_id() > max(current)
    h.message(text='2359', message_id=43)
    assert h.session.alive('preview') == current
    h.press_text('Изменить дату')
    assert h.session.alive('preview') == current
    h.press_text('Завтра')
    assert h.flow._ui().state is State.CONFIRM_SCHEDULE
    assert h.session.alive('preview')
    h.press_text(texts.BTN_CONFIRM)
    assert services.publications.get_by_post(post) is not None
    assert len(services.conn.execute('SELECT * FROM posts').fetchall()) == 1


def test_automatic_final_preview_failure_preserves_old_stack_and_explicit_retry(services):
    from test_ui_live_preview import live_harness, fail_sends
    h = live_harness(services)
    h.message(text='first', message_id=40)
    h.press_text(texts.BTN_CONTINUE)
    post = h.flow._ui().arg('post_id')
    old = h.session.alive('preview')
    # Unchanged stored cards deliberately reuse the complete visible stack.
    # A new stored version requires a new preview at final confirmation.
    from poster_bot.domain.planner import plan_post
    from poster_bot.domain.signature import SignatureConfig
    saved = h.flow._plans.get(post)
    plan = plan_post(saved.logical, SignatureConfig(
        'updated', 'https://example.com/main',
        'partner', 'https://example.com/partner')).plan
    h.flow._plans.save_with_content(post, saved.logical, plan,
                                   plan.parts[0].content, now_epoch=h.now)
    fail_sends(h, 1)
    h.message(text='2359', message_id=41)
    assert h.flow._ui().state is State.MANUAL_TIME
    assert old.issubset(h.session.visible)
    assert services.publications.get_by_post(post) is None
    h.press_text('Повторить предпросмотр')
    assert h.flow._ui().state is State.CONFIRM_SCHEDULE
    assert h.session.alive('preview').isdisjoint(old)
    assert services.publications.get_by_post(post) is None


def test_busy_manual_time_remains_editable_and_never_confirms(services):
    flow, _ = build_flow(services)
    post = services.drafts.create(text_content(), now_epoch=NOW)
    rival = services.drafts.create(text_content(), now_epoch=NOW)
    services.publications.schedule(rival, future(minutes=60), now_epoch=NOW)
    flow.draft_schedule_ask(now=NOW, post_id=post)
    reply = run(flow.text(now=NOW, text='1200', source_key='time:1'))
    assert texts.MINUTE_TAKEN_RU in reply.text
    assert texts.BTN_CONTINUE not in labels(reply)
    assert texts.BTN_CONFIRM not in labels(reply)
    assert services.publications.get_by_post(post) is None
    reply = run(flow.text(now=NOW, text='2359', source_key='time:2'))
    assert texts.BTN_CONFIRM in labels(reply)


@pytest.mark.parametrize('wall', ['29.03.2026 02:30', '25.10.2026 02:30'])
def test_auto_form_completion_keeps_dst_rejection(services, wall):
    flow, _ = build_flow(services)
    now = int(datetime(2026, 3, 28, tzinfo=ZoneInfo('UTC')).timestamp())
    services.settings.change(timezone='Europe/Berlin')
    post = services.drafts.create(text_content(), now_epoch=now)
    flow._start_time_form(now=now, payload={'purpose': 'schedule', 'post_id': post})
    reply = run(flow.text(now=now, text=wall, source_key='time:1'))
    assert texts.TIME_TZ_ISSUE in reply.text
    assert texts.BTN_CONTINUE not in labels(reply)
    assert texts.BTN_CONFIRM not in labels(reply)
    assert services.publications.get_by_post(post) is None


def test_auto_time_edit_keeps_own_minute_and_waits_for_final_confirmation(services):
    flow, _ = build_flow(services)
    post = services.drafts.create(text_content(), now_epoch=NOW)
    pub = services.publications.schedule(post, future(minutes=60), now_epoch=NOW)
    reply = flow.pub_open(now=NOW, pub_id=pub.id)
    reply = press_label(flow, reply, texts.BTN_EDIT_TIME, now=NOW)
    assert 'Изменить дату' in labels(reply) and 'Время' not in labels(reply)
    reply = run(flow.text(now=NOW, text='1200', source_key='time:1'))
    assert texts.BTN_CONFIRM in labels(reply)
    assert services.publications.get(pub.id) == pub
    press_label(flow, reply, texts.BTN_CONFIRM, now=NOW)
    assert services.publications.get(pub.id).minute_epoch == pub.minute_epoch
