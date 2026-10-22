"""Scheduling cards exercise real durable flow and services, offline only."""
from conftest import NOW, future, text_content
from ui_flow_helpers import build_flow, press_label, labels, run, reopen
from poster_bot.ui import texts
from poster_bot.ui.state import State
from ui_flow_helpers import find_token, press
from datetime import datetime, timezone
import pytest


def picker(services, now=NOW):
    flow, _ = build_flow(services)
    post = services.drafts.create(text_content(), now_epoch=now)
    flow.draft_open(now=now, post_id=post)
    return flow, post, flow.draft_schedule_ask(now=now, post_id=post)


def test_free_hours_filter_busy_past_and_require_selection(services):
    rival = services.drafts.create(text_content(), now_epoch=NOW)
    services.publications.schedule(rival, future(minutes=60), now_epoch=NOW)
    flow, post, ask = picker(services)
    panel = press_label(flow, ask, texts.BTN_NEXT_SLOT, now=NOW)
    assert 'Свободные часы' in panel.text
    assert '11:00' not in labels(panel) and '12:00' not in labels(panel)
    assert '13:00' in labels(panel)
    assert len(panel.rows) <= 8
    assert services.publications.get_by_post(post) is None
    confirm = press_label(flow, panel, '13:00', now=NOW)
    assert confirm.text.startswith(texts.CONFIRM_SUMMARY_PREFIX)
    press_label(flow, confirm, texts.BTN_CONFIRM, now=NOW)
    assert services.publications.get_by_post(post).minute_epoch == NOW + 7200


@pytest.mark.parametrize('raw', ['29-10-2026', '29.10.2026'])
def test_date_field_has_separate_grammar_and_keeps_time(services, raw):
    flow, _, ask = picker(services)
    press_label(flow, ask, texts.BTN_OWN_TIME, now=NOW)
    before = flow._ui()
    bad = run(flow.text(now=NOW, text=raw, source_key='1:201'))
    assert texts.TIME_BAD_INPUT in bad.text and flow._ui() == before
    assert not flow.input_captured('1:201')
    card = run(flow.text(now=NOW, text='1510', source_key='1:202'))
    for invalid in ('8-10', '31-02-2026', '29/10/2026', '29-10-2026x'):
        before = flow._ui()
        bad = run(flow.text(now=NOW, text=invalid, source_key='1:203'))
        assert texts.DATE_BAD_INPUT in bad.text and flow._ui() == before
        assert not flow.input_captured('1:203')
    card = run(flow.text(now=NOW, text=raw, source_key='1:204'))
    assert flow._ui().arg('time_form')['date'] == '2026-10-29'
    assert '15:10' in card.text


@pytest.mark.parametrize('raw,day', [('29-03-2026', '2026-03-29'),
                                   ('25-10-2026', '2026-10-25')])
def test_changed_date_clears_time_only_for_dst_gap_or_fold(services, raw, day):
    now = int(datetime(2026, 3, 28, tzinfo=timezone.utc).timestamp())
    flow, _, ask = picker(services, now)
    services.settings.change(timezone='Europe/Berlin')
    card = press_label(flow, ask, texts.BTN_OWN_TIME, now=now)
    card = run(flow.text(now=now, text='230'))
    card = run(flow.text(now=now, text=raw))
    form = flow._ui().arg('time_form')
    assert form['date'] == day and form['time'] is None
    assert form['field'] == 'time' and texts.TIME_TZ_ISSUE in card.text


def test_custom_fields_default_date_independent_durable(services, tmp_path):
    flow, post, ask = picker(services)
    card = press_label(flow, ask, texts.BTN_OWN_TIME, now=NOW)
    assert 'Дата: Сегодня (21.10.2026)' in card.text
    assert 'Время: не выбрано' in card.text
    card = run(flow.text(now=NOW, text='15:37', source_key='1:99'))
    assert flow.input_captured('1:99')
    assert '15:37' in card.text
    services, flow, _ = reopen(services, tmp_path)
    edit = press_label(flow, card, 'Дата', now=NOW)
    card = press_label(flow, edit, 'Завтра', now=NOW)
    assert '22.10.2026' in card.text and '15:37' in card.text
    card = press_label(flow, card, 'Дата', now=NOW)
    card = press_label(flow, card, 'Сегодня', now=NOW)
    assert '21.10.2026' in card.text and '15:37' in card.text
    card = press_label(flow, card, 'Дата', now=NOW)
    card = press_label(flow, card, 'Завтра', now=NOW)
    confirm = press_label(flow, card, 'Продолжить', now=NOW)
    assert '22.10.2026 15:37' in confirm.text
    assert services.publications.get_by_post(post) is None
    press_label(flow, confirm, texts.BTN_CONFIRM, now=NOW)
    assert services.publications.get_by_post(post) is not None


def test_empty_today_navigation_pagination_range_and_stale_tokens(services):
    flow, post, ask = picker(services)
    services.settings.change(slot_start_hour=0, slot_end_hour=0)
    panel = press_label(flow, ask, texts.BTN_NEXT_SLOT, now=NOW)
    assert 'Свободных часов нет' in panel.text
    assert '← День' not in labels(panel)
    old = find_token(panel, 'Сегодня')
    tomorrow = press_label(flow, panel, 'День →', now=NOW)
    assert '22.10.2026' in tomorrow.text and '00:00' in labels(tomorrow)
    ui = flow._ui()
    stale = press(flow, old, now=NOW)
    assert stale.stale_callback and flow._ui() == ui
    services.settings.change(slot_end_hour=23)
    tomorrow = press_label(flow, tomorrow, 'Сегодня', now=NOW)
    tomorrow = press_label(flow, tomorrow, 'День →', now=NOW)
    assert len(tomorrow.rows) <= 8 and 'Страница 1/2' in tomorrow.text
    page2 = press_label(flow, tomorrow, 'Часы ›', now=NOW)
    assert 'Страница 2/2' in page2.text
    assert '00:00' not in labels(page2) and '23:00' in labels(page2)
    assert len(page2.rows) <= 8
    today = press_label(flow, page2, 'Сегодня', now=NOW)
    assert '21.10.2026' in today.text and '← День' not in labels(today)
    assert services.publications.get_by_post(post) is None


@pytest.mark.parametrize('occupied', [True, False])
def test_hour_click_rechecks_current_availability(services, occupied):
    flow, post, ask = picker(services)
    panel = press_label(flow, ask, texts.BTN_NEXT_SLOT, now=NOW)
    if occupied:
        rival = services.drafts.create(text_content(), now_epoch=NOW)
        services.publications.schedule(rival, future(minutes=60), now_epoch=NOW)
    now = NOW if occupied else NOW + 3600
    refreshed = press_label(flow, panel, '12:00', now=now)
    assert 'Этот час уже недоступен' in refreshed.text
    assert '12:00' not in labels(refreshed)
    assert services.publications.get_by_post(post) is None


@pytest.mark.parametrize('zone,day', [('Europe/Moscow', '22.10.2026'),
                                   ('America/New_York', '21.10.2026')])
def test_today_uses_configured_timezone_near_midnight(services, zone, day):
    now = int(datetime(2026, 10, 21, 23, 30, tzinfo=timezone.utc).timestamp())
    flow, _, ask = picker(services, now)
    services.settings.change(timezone=zone)
    form = press_label(flow, ask, texts.BTN_OWN_TIME, now=now)
    assert f'Дата: Сегодня ({day})' in form.text
    flow, _, ask = picker(services, now)
    panel = press_label(flow, ask, texts.BTN_NEXT_SLOT, now=now)
    assert day in panel.text


def test_missing_invalid_fields_and_past_never_queue(services):
    flow, post, ask = picker(services)
    card = press_label(flow, ask, texts.BTN_OWN_TIME, now=NOW)
    missing = press_label(flow, card, 'Продолжить', now=NOW)
    assert 'Выберите время' in missing.text
    edit = press_label(flow, missing, 'Дата', now=NOW)
    before = flow._ui()
    bad = run(flow.text(now=NOW, text='31.02.2026', source_key='1:1'))
    assert texts.DATE_BAD_INPUT in bad.text and flow._ui() == before
    assert not flow.input_captured('1:1')
    bad = run(flow.text(now=NOW, text='20.10.2026', source_key='1:2'))
    assert texts.TIME_NOT_FUTURE in bad.text and flow._ui() == before
    card = run(flow.text(now=NOW, text='21.10.2026', source_key='1:3'))
    edit = press_label(flow, card, 'Время', now=NOW)
    before = flow._ui()
    bad = run(flow.text(now=NOW, text='25:61', source_key='1:4'))
    assert flow._ui() == before and not flow.input_captured('1:4')
    card = run(flow.text(now=NOW, text='09:00', source_key='1:5'))
    refused = press_label(flow, card, 'Продолжить', now=NOW)
    assert texts.TIME_NOT_FUTURE in refused.text and '09:00' in refused.text
    assert services.publications.get_by_post(post) is None
    assert services.manual_times.recent() == []


def test_own_time_busy_and_replay_preserve_card(services):
    flow, post, ask = picker(services)
    rival = services.drafts.create(text_content(), now_epoch=NOW)
    services.publications.schedule(rival, future(minutes=60), now_epoch=NOW)
    card = press_label(flow, ask, texts.BTN_OWN_TIME, now=NOW)
    old = find_token(card, 'Дата')
    card = run(flow.text(now=NOW, text='12:00', source_key='1:6'))
    before = flow._ui()
    stale = press(flow, old, now=NOW)
    assert stale.stale_callback and flow._ui() == before
    replay = run(flow.text(now=NOW, text='12:00', source_key='1:6'))
    assert '12:00' in replay.text and flow._ui() == before
    refused = press_label(flow, card, 'Продолжить', now=NOW)
    assert texts.MINUTE_TAKEN_RU in refused.text
    assert services.publications.get_by_post(post) is None


@pytest.mark.parametrize('wall', ['29.03.2026 02:30', '25.10.2026 02:30'])
def test_custom_dst_gap_and_fold_rejected(services, wall):
    now = int(datetime(2026, 3, 28, tzinfo=timezone.utc).timestamp())
    flow, post, ask = picker(services, now)
    services.settings.change(timezone='Europe/Berlin')
    press_label(flow, ask, texts.BTN_OWN_TIME, now=now)
    card = run(flow.text(now=now, text=wall))
    refused = press_label(flow, card, 'Продолжить', now=now)
    assert texts.TIME_TZ_ISSUE in refused.text and '02:30' in refused.text
    assert services.publications.get_by_post(post) is None


def test_reschedule_can_keep_own_minute_and_only_confirm_mutates(services):
    flow, post, _ = picker(services)
    pub = services.publications.schedule(post, future(minutes=60), now_epoch=NOW)
    view = flow.pub_open(now=NOW, pub_id=pub.id)
    press_label(flow, view, texts.BTN_EDIT_TIME, now=NOW)
    card = run(flow.text(now=NOW, text='12:00'))
    confirm = press_label(flow, card, 'Продолжить', now=NOW)
    assert confirm.text.startswith(texts.CONFIRM_SUMMARY_PREFIX)
    assert services.publications.get(pub.id) == pub
    done = press_label(flow, confirm, texts.BTN_CONFIRM, now=NOW)
    assert texts.TIME_EDITED in done.text
    assert services.publications.get(pub.id).minute_epoch == pub.minute_epoch


def test_dispatcher_field_capture_deletion_and_stale_toast(services, monkeypatch):
    from test_ui_handlers_offline import _Harness, OWNER
    h = _Harness(services)
    h.message(text='/start')
    h.press_text(texts.MENU_NEW_POST)
    h.message(text='post', message_id=40)
    h.press_text(texts.BTN_CONTINUE)
    h.press_text(texts.BTN_SCHEDULE)
    h.press_text(texts.BTN_OWN_TIME)
    receipts = []
    h.session.before_delete = lambda mid: receipts.append((mid, h.ui.has_effect(f'{OWNER}:{mid}')))
    h.message(text='14:35', message_id=41)
    assert (41, True) in receipts and 41 not in h.session.visible
    h.press_text('Дата')
    h.message(text='31.02.2026', message_id=42)
    assert 42 in h.session.visible and not h.ui.has_effect(f'{OWNER}:42')
    old_card = h.card_id()
    before = h.ui.get()
    def unavailable(*args, **kwargs):
        raise RuntimeError('isolated persistence failure')
    monkeypatch.setattr(h.flow._state, 'set', unavailable)
    with pytest.raises(RuntimeError, match='persistence failure'):
        h.message(text='22.10.2026', message_id=43)
    assert 43 in h.session.visible and h.card_id() == old_card and h.ui.get() == before


@pytest.mark.parametrize('raw', ['810', '0810', '8-10', '08-10', '8:10',
                                '08:10', '8.10', '08.10', '8 10', '08 10'])
def test_clock_requested_formats(raw):
    from poster_bot.ui.times_input import parse_clock
    assert parse_clock(raw) == (8, 10)


@pytest.mark.parametrize('raw', ['2410', '2360', '8-1', '08:100', '8  10',
                                '29-10-2026', '8/10', '810x', '-810', '8:10 pm'])
def test_clock_rejects_invalid_or_date(raw):
    from poster_bot.ui.times_input import parse_clock
    assert parse_clock(raw) is None


def test_own_time_immediately_waits_then_custom_hyphen_date_preserves_time(services):
    flow, post, ask = picker(services)
    card = press_label(flow, ask, texts.BTN_OWN_TIME, now=NOW)
    assert flow._ui().arg('time_form')['field'] == 'time'
    card = run(flow.text(now=NOW, text='15 37', source_key='1:90'))
    assert flow.input_captured('1:90')
    assert flow._ui().arg('time_form')['field'] == 'date'
    card = press_label(flow, card, 'Дата', now=NOW)
    assert 'ДД-ММ-ГГГГ' in card.text
    card = run(flow.text(now=NOW, text='29-10-2026', source_key='1:91'))
    assert '15:37' in card.text and flow.input_captured('1:91')
    confirm = press_label(flow, card, 'Продолжить', now=NOW)
    assert '29.10.2026 15:37' in confirm.text
    assert services.publications.get_by_post(post) is None


@pytest.mark.parametrize('raw', ['810', '0810', '8-10', '08-10', '8:10',
                                '08:10', '8.10', '08.10', '8 10', '08 10'])
@pytest.mark.parametrize('zone,day', [('Europe/Moscow', '2026-10-22'),
                                   ('America/New_York', '2026-10-21')])
def test_dispatcher_own_time_first_text_uses_today_in_configured_zone(services, raw, zone, day):
    from test_ui_handlers_offline import _Harness, OWNER
    h = _Harness(services)
    services.settings.change(timezone=zone)
    h.now = int(datetime(2026, 10, 21, 23, 30, tzinfo=timezone.utc).timestamp())
    h.message(text='/start')
    h.press_text(texts.MENU_NEW_POST)
    h.message(text='post', message_id=40)
    h.press_text(texts.BTN_CONTINUE)
    h.press_text(texts.BTN_SCHEDULE)
    h.press_text(texts.BTN_OWN_TIME)
    h.message(text=raw, message_id=41)
    form = h.flow._ui().arg('time_form')
    assert form['date'] == day and form['time'] == '08:10'
    assert h.ui.has_effect(f'{OWNER}:41') and 41 not in h.session.visible
    assert not services.publications.nearest_planned()


def test_old_picker_back_cannot_destroy_current_card(services):
    flow, _, ask = picker(services)
    old = find_token(ask, texts.BTN_BACK)
    card = press_label(flow, ask, texts.BTN_OWN_TIME, now=NOW)
    before = flow._ui()
    stale = press(flow, old, now=NOW)
    assert stale.stale_callback and flow._ui() == before
    # New entry resets both fields to today's date, not the abandoned choice.
    card = run(flow.text(now=NOW, text='25.10.2026 14:30'))
    view = press_label(flow, card, 'Назад', now=NOW)
    ask = press_label(flow, view, texts.BTN_SCHEDULE, now=NOW)
    fresh = press_label(flow, ask, texts.BTN_OWN_TIME, now=NOW)
    assert 'Дата: Сегодня (21.10.2026)' in fresh.text and 'Время: не выбрано' in fresh.text


@pytest.mark.parametrize('status', ['scheduled', 'suspended', 'sending', 'uncertain', 'missed'])
def test_every_reserving_status_hides_hour(services, status):
    flow, _, ask = picker(services)
    rival = services.drafts.create(text_content(), now_epoch=NOW)
    pub = services.publications.schedule(rival, future(minutes=60), now_epoch=NOW)
    # Isolated fixture models storage states; production transitions stay owned
    # by services and their independent tests.
    with services.conn:
        services.conn.execute('UPDATE publications SET status=? WHERE id=?', (status, pub.id))
    panel = press_label(flow, ask, texts.BTN_NEXT_SLOT, now=NOW)
    assert '12:00' not in labels(panel)


def test_generated_hours_keep_existing_dst_policy(services):
    from poster_bot.domain.time import day_slots
    from datetime import date
    now = int(datetime(2026, 3, 29, tzinfo=timezone.utc).timestamp())
    flow, _, ask = picker(services, now)
    services.settings.change(timezone='Europe/Berlin')
    panel = press_label(flow, ask, texts.BTN_NEXT_SLOT, now=now)
    assert '02:00' not in labels(panel)
    # Domain chooses fold=0 for generated slots, never a second 02:00 chip.
    slots = flow._free_hours(date(2026, 10, 25), now)
    assert slots == list(day_slots(date(2026, 10, 25), 'Europe/Berlin', 0, 23))
    assert sum(s.local('Europe/Berlin').hour == 2 for s in slots) == 1


@pytest.mark.parametrize('raw', ['810', '0810', '8-10', '08-10', '8:10',
                                '08:10', '8.10', '08.10', '8 10', '08 10'])
@pytest.mark.parametrize('zone,day', [('Europe/Moscow', '2026-10-22'),
                                   ('America/New_York', '2026-10-21')])
def test_dispatcher_initial_picker_accepts_direct_time(services, raw, zone, day):
    from test_ui_handlers_offline import _Harness, OWNER
    h = _Harness(services)
    services.settings.change(timezone=zone)
    h.now = int(datetime(2026, 10, 21, 23, 30, tzinfo=timezone.utc).timestamp())
    h.message(text='/start')
    h.press_text(texts.MENU_NEW_POST)
    h.message(text='original post', message_id=40)
    h.press_text(texts.BTN_CONTINUE)
    h.press_text(texts.BTN_SCHEDULE)
    old_card = h.card_id()
    receipts = []
    h.session.before_delete = lambda mid: receipts.append((mid, h.ui.has_effect(f'{OWNER}:{mid}')))
    h.message(text=raw, message_id=41)
    ui = h.ui.get()
    assert ui.state is State.MANUAL_TIME
    assert ui.arg('time_form') == {'date': day, 'time': '08:10', 'field': 'date'}
    assert (41, True) in receipts and 41 not in h.session.visible
    assert h.card_id() > old_card
    assert 'Завтра' in str(h.cards()[-1]['reply_markup'])
    assert not services.publications.nearest_planned()
    assert services.drafts.get_content(ui.arg('post_id')).text == 'original post'


def test_direct_past_today_requires_explicit_tomorrow_and_final_confirm(services):
    flow, post, ask = picker(services)
    card = run(flow.text(now=NOW, text='810', source_key='1:301'))
    assert texts.TIME_NOT_FUTURE in card.text and 'Завтра' in labels(card)
    assert flow._ui().arg('time_form')['date'] == '2026-10-21'
    refused = press_label(flow, card, 'Продолжить', now=NOW)
    assert texts.TIME_NOT_FUTURE in refused.text
    assert services.publications.get_by_post(post) is None
    card = press_label(flow, refused, 'Завтра', now=NOW)
    confirm = press_label(flow, card, 'Продолжить', now=NOW)
    assert '22.10.2026 08:10' in confirm.text
    assert services.publications.get_by_post(post) is None
    press_label(flow, confirm, texts.BTN_CONFIRM, now=NOW)
    assert services.publications.get_by_post(post) is not None


@pytest.mark.parametrize('raw', ['29-10-2026', 'hello', '2360', '8/10'])
def test_direct_invalid_time_preserves_picker_and_source(services, raw):
    flow, post, ask = picker(services)
    before = flow._ui()
    reply = run(flow.text(now=NOW, text=raw, source_key='1:302'))
    assert flow._ui() == before and not reply.input_accepted
    assert texts.TIME_BAD_INPUT in reply.text and '810' in reply.text
    assert texts.BTN_OWN_TIME in labels(reply)
    assert not flow.input_captured('1:302')
    assert services.publications.get_by_post(post) is None


def test_direct_capture_atomic_failure_and_restart_replay(services, tmp_path, monkeypatch):
    flow, post, ask = picker(services)
    before = flow._ui()
    original_set = flow._state.set
    def fail(*args, **kwargs):
        raise RuntimeError('isolated persistence failure')
    monkeypatch.setattr(flow._state, 'set', fail)
    with pytest.raises(RuntimeError, match='persistence failure'):
        run(flow.text(now=NOW, text='810', source_key='1:303'))
    assert flow._ui() == before and not flow.input_captured('1:303')
    monkeypatch.setattr(flow._state, 'set', original_set)
    card = run(flow.text(now=NOW, text='810', source_key='1:303'))
    services, flow, _ = reopen(services, tmp_path)
    card = press_label(flow, card, 'Завтра', now=NOW)
    before = flow._ui()
    replay = run(flow.text(now=NOW, text='810', source_key='1:303'))
    assert flow._ui() == before and '22.10.2026' in replay.text
    assert services.drafts.list_drafts() == [post]
    assert services.publications.get_by_post(post) is None


def test_dispatcher_direct_invalid_and_failed_capture_keep_input(services, monkeypatch):
    from test_ui_handlers_offline import _Harness, OWNER
    h = _Harness(services)
    h.message(text='/start')
    h.press_text(texts.MENU_NEW_POST)
    h.message(text='post', message_id=40)
    h.press_text(texts.BTN_CONTINUE)
    h.press_text(texts.BTN_SCHEDULE)
    before = h.ui.get()
    h.message(text='29-10-2026', message_id=41)
    assert h.ui.get() == before
    assert 41 in h.session.visible and not h.ui.has_effect(f'{OWNER}:41')
    assert texts.TIME_BAD_INPUT in h.last_text()
    card_id = h.card_id()
    def fail(*args, **kwargs):
        raise RuntimeError('isolated persistence failure')
    monkeypatch.setattr(h.flow._state, 'set', fail)
    with pytest.raises(RuntimeError, match='persistence failure'):
        h.message(text='810', message_id=42)
    assert h.ui.get() == before and h.card_id() == card_id
    assert 42 in h.session.visible and not h.ui.has_effect(f'{OWNER}:42')


def test_direct_picker_survives_restart_and_does_not_leak_to_another_draft(services, tmp_path):
    flow, post, _ = picker(services)
    services, flow, _ = reopen(services, tmp_path)
    card = run(flow.text(now=NOW, text='1510', source_key='1:304'))
    assert flow._ui().arg('post_id') == post and '15:10' in card.text
    other = services.drafts.create(text_content('other post'), now_epoch=NOW)
    flow.draft_open(now=NOW, post_id=other)
    flow.draft_schedule_ask(now=NOW, post_id=other)
    card = run(flow.text(now=NOW, text='1610', source_key='1:305'))
    assert flow._ui().arg('post_id') == other and '16:10' in card.text
    assert services.publications.get_by_post(post) is None
    assert services.publications.get_by_post(other) is None


def test_time_edit_keeps_publication_version_and_target_after_direct_draft_input(services):
    flow, post, _ = picker(services)
    run(flow.text(now=NOW, text='1510', source_key='1:306'))
    pub = services.publications.schedule(post, future(minutes=60), now_epoch=NOW)
    view = flow.pub_open(now=NOW, pub_id=pub.id)
    press_label(flow, view, texts.BTN_EDIT_TIME, now=NOW)
    card = run(flow.text(now=NOW, text='1610', source_key='1:307'))
    ui = flow._ui()
    assert ui.arg('pub_id') == pub.id and ui.arg('expected_version') == pub.version
    assert ui.arg('purpose') == 'edit_time' and not ui.arg('schedule_picker')
    assert services.publications.get(pub.id) == pub
    confirm = press_label(flow, card, 'Продолжить', now=NOW)
    press_label(flow, confirm, texts.BTN_CONFIRM, now=NOW)
    assert services.publications.get(pub.id).id == pub.id
