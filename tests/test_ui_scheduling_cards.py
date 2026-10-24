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
    panel = press_label(flow, ask, texts.BTN_DAY_SLOTS, now=NOW)
    assert 'Свободные часы' in panel.text
    assert '11:00' not in labels(panel) and '12:00' not in labels(panel)
    assert '13:00' in labels(panel)
    assert len(panel.rows) <= 8
    assert services.publications.get_by_post(post) is None
    confirm = press_label(flow, panel, '13:00', now=NOW)
    assert confirm.text == '❗ Публикация будет запланирована на 13:00 21.10.2026 ❗'
    press_label(flow, confirm, texts.BTN_CONFIRM, now=NOW)
    assert services.publications.get_by_post(post).minute_epoch == NOW + 7200


@pytest.mark.parametrize('raw', ['29-10-2026', '29.10.2026'])
def test_date_field_has_separate_grammar_and_keeps_time(services, raw):
    flow, _, ask = picker(services)
    before = flow._ui()
    bad = run(flow.text(now=NOW, text=raw, source_key='1:201'))
    assert texts.TIME_BAD_INPUT in bad.text and flow._ui() == before
    assert not flow.input_captured('1:201')
    card = run(flow.text(now=NOW, text='1510', source_key='1:202'))
    card = press_label(flow, card, 'Изменить дату', now=NOW)
    # Clock input remains reachable even while editing the date: this is a
    # clock, not an invalid date. It must not silently roll into tomorrow.
    card = run(flow.text(now=NOW, text='8-10', source_key='1:205'))
    assert flow._ui().arg('time_form')['time'] == '08:10'
    assert flow.input_captured('1:205') and texts.TIME_NOT_FUTURE in card.text
    card = run(flow.text(now=NOW, text='1510'))
    card = press_label(flow, card, 'Изменить дату', now=NOW)
    for invalid in ('31-02-2026', '29/10/2026', '29-10-2026x'):
        before = flow._ui()
        bad = run(flow.text(now=NOW, text=invalid, source_key='1:203'))
        assert texts.DATE_BAD_INPUT in bad.text and flow._ui() == before
        assert not flow.input_captured('1:203')
    card = run(flow.text(now=NOW, text=raw, source_key='1:204'))
    assert flow._ui().state is State.CONFIRM_SCHEDULE
    assert card.text == '❗ Публикация будет запланирована на 15:10 29.10.2026 ❗'


@pytest.mark.parametrize('raw,day', [('29-03-2026', '2026-03-29'),
                                   ('25-10-2026', '2026-10-25')])
def test_changed_date_clears_time_only_for_dst_gap_or_fold(services, raw, day):
    now = int(datetime(2026, 3, 28, tzinfo=timezone.utc).timestamp())
    flow, _, ask = picker(services, now)
    services.settings.change(timezone='Europe/Berlin')
    card = run(flow.text(now=now, text='230'))
    card = press_label(flow, card, 'Изменить дату', now=now)
    card = run(flow.text(now=now, text=raw))
    form = flow._ui().arg('time_form')
    assert form['date'] == day and form['time'] is None
    assert form['field'] == 'time' and texts.TIME_TZ_ISSUE in card.text


def test_custom_fields_default_date_independent_durable(services, tmp_path):
    flow, post, ask = picker(services)
    assert texts.BTN_OWN_TIME not in labels(ask)
    assert services.publications.get_by_post(post) is None
    card = run(flow.text(now=NOW, text='15:37', source_key='1:99'))
    assert flow.input_captured('1:99')
    assert '15:37' in card.text
    services, flow, _ = reopen(services, tmp_path)
    edit = press_label(flow, card, 'Изменить дату', now=NOW)
    card = press_label(flow, edit, 'Завтра', now=NOW)
    assert '22.10.2026' in card.text and '15:37' in card.text
    card = press_label(flow, card, 'Изменить дату', now=NOW)
    card = press_label(flow, card, 'Сегодня', now=NOW)
    assert '21.10.2026' in card.text and '15:37' in card.text
    card = press_label(flow, card, 'Изменить дату', now=NOW)
    confirm = press_label(flow, card, 'Завтра', now=NOW)
    assert confirm.text == '❗ Публикация будет запланирована на 15:37 22.10.2026 ❗'
    assert services.publications.get_by_post(post) is None
    press_label(flow, confirm, texts.BTN_CONFIRM, now=NOW)
    assert services.publications.get_by_post(post) is not None


def test_empty_today_navigation_pagination_range_and_stale_tokens(services):
    flow, post, ask = picker(services)
    services.settings.change(slot_start_hour=0, slot_end_hour=0)
    panel = press_label(flow, ask, texts.BTN_DAY_SLOTS, now=NOW)
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
    panel = press_label(flow, ask, texts.BTN_DAY_SLOTS, now=NOW)
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
    form = run(flow.text(now=now, text='0010'))
    assert f'Дата: Сегодня ({day})' in form.text
    flow, _, ask = picker(services, now)
    panel = press_label(flow, ask, texts.BTN_DAY_SLOTS, now=now)
    assert day in panel.text


def test_missing_invalid_fields_and_past_never_queue(services):
    flow, post, ask = picker(services)
    assert texts.BTN_CONTINUE not in labels(ask)
    card = run(flow.text(now=NOW, text='09:00'))
    assert texts.TIME_NOT_FUTURE in card.text
    edit = press_label(flow, card, 'Изменить дату', now=NOW)
    before = flow._ui()
    bad = run(flow.text(now=NOW, text='31.02.2026', source_key='1:1'))
    assert texts.DATE_BAD_INPUT in bad.text and flow._ui() == before
    assert not flow.input_captured('1:1')
    bad = run(flow.text(now=NOW, text='20.10.2026', source_key='1:2'))
    assert texts.TIME_NOT_FUTURE in bad.text and flow._ui() == before
    card = run(flow.text(now=NOW, text='21.10.2026', source_key='1:3'))
    before = flow._ui()
    bad = run(flow.text(now=NOW, text='25:61', source_key='1:4'))
    assert flow._ui() == before and not flow.input_captured('1:4')
    refused = run(flow.text(now=NOW, text='09:00', source_key='1:5'))
    assert texts.TIME_NOT_FUTURE in refused.text and '09:00' in refused.text
    assert services.publications.get_by_post(post) is None
    assert services.manual_times.recent() == []


def test_own_time_busy_and_replay_preserve_card(services):
    flow, post, ask = picker(services)
    rival = services.drafts.create(text_content(), now_epoch=NOW)
    services.publications.schedule(rival, future(minutes=60), now_epoch=NOW)
    old = find_token(ask, texts.BTN_BACK)
    card = run(flow.text(now=NOW, text='12:00', source_key='1:6'))
    before = flow._ui()
    stale = press(flow, old, now=NOW)
    assert stale.stale_callback and flow._ui() == before
    replay = run(flow.text(now=NOW, text='12:00', source_key='1:6'))
    assert '12:00' in replay.text and flow._ui() == before
    assert texts.MINUTE_TAKEN_RU in card.text
    assert texts.BTN_CONFIRM not in labels(card)
    assert services.publications.get_by_post(post) is None


@pytest.mark.parametrize('wall', ['29.03.2026 02:30', '25.10.2026 02:30'])
def test_custom_dst_gap_and_fold_rejected(services, wall):
    now = int(datetime(2026, 3, 28, tzinfo=timezone.utc).timestamp())
    flow, post, ask = picker(services, now)
    services.settings.change(timezone='Europe/Berlin')
    card = run(flow.text(now=now, text='230'))
    card = run(flow.text(now=now, text=wall))
    assert texts.TIME_TZ_ISSUE in card.text and '02:30' in card.text
    assert texts.BTN_CONFIRM not in labels(card)
    assert services.publications.get_by_post(post) is None


def test_reschedule_can_keep_own_minute_and_only_confirm_mutates(services):
    flow, post, _ = picker(services)
    pub = services.publications.schedule(post, future(minutes=60), now_epoch=NOW)
    view = flow.pub_open(now=NOW, pub_id=pub.id)
    press_label(flow, view, texts.BTN_EDIT_TIME, now=NOW)
    confirm = run(flow.text(now=NOW, text='12:00'))
    assert confirm.text == '❗ Публикация будет запланирована на 12:00 21.10.2026 ❗'
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
    assert h.ui.get().arg('schedule_picker')
    receipts = []
    h.session.before_delete = lambda mid: receipts.append((mid, h.ui.has_effect(f'{OWNER}:{mid}')))
    h.message(text='14:35', message_id=41)
    assert (41, True) in receipts and 41 not in h.session.visible
    h.press_text('Изменить дату')
    h.message(text='31.02.2026', message_id=42)
    assert 42 not in h.session.visible and not h.ui.has_effect(f'{OWNER}:42')
    assert h.flow.input_discarded(f'{OWNER}:42')
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
    assert flow._ui().arg('schedule_picker')
    assert texts.BTN_OWN_TIME not in labels(ask)
    card = run(flow.text(now=NOW, text='15 37', source_key='1:90'))
    assert flow.input_captured('1:90')
    assert flow._ui().state is State.CONFIRM_SCHEDULE
    card = press_label(flow, card, 'Изменить дату', now=NOW)
    assert 'ДД-ММ-ГГГГ' in card.text
    card = run(flow.text(now=NOW, text='29-10-2026', source_key='1:91'))
    assert '15:37' in card.text and flow.input_captured('1:91')
    assert card.text == '❗ Публикация будет запланирована на 15:37 29.10.2026 ❗'
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
    assert h.ui.get().arg('schedule_picker')
    h.message(text=raw, message_id=41)
    ui = h.ui.get()
    if zone == 'Europe/Moscow':
        assert ui.state is State.CONFIRM_SCHEDULE
        assert h.last_text() == '❗ Публикация будет запланирована на 08:10 22.10.2026 ❗'
        assert datetime.fromtimestamp(ui.arg('minute_epoch'), timezone.utc).isoformat() == '2026-10-22T05:10:00+00:00'
    else:
        form = ui.arg('time_form')
        assert form['date'] == day and form['time'] == '08:10'
        assert texts.TIME_NOT_FUTURE in h.last_text()
    assert h.ui.has_effect(f'{OWNER}:41') and 41 not in h.session.visible
    assert not services.publications.nearest_planned()


@pytest.mark.parametrize('clock', ['810', '1430'])
def test_old_picker_back_cannot_destroy_current_card(services, clock):
    flow, _, ask = picker(services)
    old = find_token(ask, texts.BTN_BACK)
    card = run(flow.text(now=NOW, text=clock))
    before = flow._ui()
    stale = press(flow, old, now=NOW)
    assert stale.stale_callback and flow._ui() == before
    # New entry resets both fields to today's date, not the abandoned choice.
    card = run(flow.text(now=NOW, text='25.10.2026 14:30'))
    card = press_label(flow, card, 'Изменить дату', now=NOW)
    view = press_label(flow, card, texts.BTN_BACK, now=NOW)
    ask = press_label(flow, view, texts.BTN_SCHEDULE, now=NOW)
    assert flow._ui().arg('schedule_picker') and flow._ui().arg('time_form') is None
    fresh = run(flow.text(now=NOW, text='810'))
    assert 'Дата: Сегодня (21.10.2026)' in fresh.text and '08:10' in fresh.text


@pytest.mark.parametrize('status', ['scheduled', 'suspended', 'sending', 'uncertain', 'missed'])
def test_every_reserving_status_hides_hour(services, status):
    flow, _, ask = picker(services)
    rival = services.drafts.create(text_content(), now_epoch=NOW)
    pub = services.publications.schedule(rival, future(minutes=60), now_epoch=NOW)
    # Isolated fixture models storage states; production transitions stay owned
    # by services and their independent tests.
    with services.conn:
        services.conn.execute('UPDATE publications SET status=? WHERE id=?', (status, pub.id))
    panel = press_label(flow, ask, texts.BTN_DAY_SLOTS, now=NOW)
    assert '12:00' not in labels(panel)


def test_generated_hours_keep_existing_dst_policy(services):
    from poster_bot.domain.time import day_slots
    from datetime import date
    now = int(datetime(2026, 3, 29, tzinfo=timezone.utc).timestamp())
    flow, _, ask = picker(services, now)
    services.settings.change(timezone='Europe/Berlin')
    panel = press_label(flow, ask, texts.BTN_DAY_SLOTS, now=now)
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
    post_id = h.ui.get().arg('post_id')
    assert post_id in services.drafts.list_drafts()
    assert h.ui.get().arg('schedule_picker')
    old_card = h.card_id()
    receipts = []
    h.session.before_delete = lambda mid: receipts.append((mid, h.ui.has_effect(f'{OWNER}:{mid}')))
    h.message(text=raw, message_id=41)
    ui = h.ui.get()
    if zone == 'Europe/Moscow':
        assert ui.state is State.CONFIRM_SCHEDULE
        assert h.last_text() == '❗ Публикация будет запланирована на 08:10 22.10.2026 ❗'
        assert 'Изменить дату' in h.last_labels()
    else:
        assert ui.state is State.MANUAL_TIME
        assert ui.arg('time_form') == {'date': day, 'time': '08:10', 'field': 'date'}
        assert 'Завтра' in h.last_labels()
    assert (41, True) in receipts and 41 not in h.session.visible
    assert h.card_id() > old_card
    assert not services.publications.nearest_planned()
    assert ui.arg('post_id') == post_id
    assert services.drafts.get_content(post_id).text == 'original post'


def test_direct_past_today_requires_explicit_tomorrow_and_final_confirm(services):
    flow, post, ask = picker(services)
    card = run(flow.text(now=NOW, text='810', source_key='1:301'))
    assert texts.TIME_NOT_FUTURE in card.text and 'Завтра' in labels(card)
    assert flow._ui().arg('time_form')['date'] == '2026-10-21'
    assert texts.BTN_CONTINUE not in labels(card) and texts.BTN_CONFIRM not in labels(card)
    assert services.publications.get_by_post(post) is None
    confirm = press_label(flow, card, 'Завтра', now=NOW)
    assert confirm.text == '❗ Публикация будет запланирована на 08:10 22.10.2026 ❗'
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
    assert texts.BTN_OWN_TIME not in labels(reply)
    assert texts.BTN_DAY_SLOTS in labels(reply)
    assert sum(' → ' in label for label in labels(reply)) >= 12
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
    assert h.ui.get().arg('schedule_picker')
    before = h.ui.get()
    h.message(text='29-10-2026', message_id=41)
    assert h.ui.get() == before
    assert 41 not in h.session.visible and not h.ui.has_effect(f'{OWNER}:41')
    assert h.flow.input_discarded(f'{OWNER}:41')
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
    confirm = run(flow.text(now=NOW, text='1610', source_key='1:307'))
    ui = flow._ui()
    assert ui.arg('pub_id') == pub.id and ui.arg('expected_version') == pub.version
    assert ui.arg('purpose') == 'edit_time_confirm' and not ui.arg('schedule_picker')
    assert services.publications.get(pub.id) == pub
    assert confirm.text == '❗ Публикация будет запланирована на 16:10 21.10.2026 ❗'
    press_label(flow, confirm, texts.BTN_CONFIRM, now=NOW)
    assert services.publications.get(pub.id).id == pub.id


def test_initial_picker_has_twelve_chronological_candidates_and_day_navigation(services):
    flow, post, ask = picker(services)
    candidates = [label for label in labels(ask) if ' → ' in label]
    assert len(candidates) >= 12
    instants = [datetime.strptime(label.split(' → ')[1], '%d.%m.%Y %H:%M')
                for label in candidates]
    assert instants == sorted(set(instants))
    assert texts.BTN_OWN_TIME not in labels(ask)
    panel = press_label(flow, ask, texts.BTN_DAY_SLOTS, now=NOW)
    assert 'День →' in labels(panel)
    assert services.publications.get_by_post(post) is None


@pytest.mark.parametrize('screen', ['day', 'confirmation', 'date'])
def test_direct_clock_remains_reachable_without_manual_or_continue_buttons(services, screen):
    flow, post, ask = picker(services)
    if screen == 'day':
        card = press_label(flow, ask, texts.BTN_DAY_SLOTS, now=NOW)
        card = press_label(flow, card, 'День →', now=NOW)
        day = '22.10.2026'
    else:
        card = run(flow.text(now=NOW, text='1510'))
        if screen == 'date':
            card = press_label(flow, card, 'Изменить дату', now=NOW)
        day = '21.10.2026'
    assert texts.BTN_OWN_TIME not in labels(card)
    assert texts.BTN_CONTINUE not in labels(card)
    confirm = run(flow.text(now=NOW, text='16:37', source_key='1:501'))
    assert confirm.text == f'❗ Публикация будет запланирована на 16:37 {day} ❗'
    assert flow.input_captured('1:501')
    assert flow._ui().state is State.CONFIRM_SCHEDULE
    assert flow._ui().arg('post_id') == post
    assert services.publications.get_by_post(post) is None
    assert texts.BTN_CONFIRM in labels(confirm)


@pytest.mark.parametrize('origin', ['draft', 'picker', 'confirmation', 'publication'])
def test_old_screen_back_preserves_new_scheduling_confirmation(services, origin):
    flow, post, ask = picker(services)
    if origin == 'draft':
        old_view = flow.draft_open(now=NOW, post_id=post)
    elif origin == 'confirmation':
        old_view = run(flow.text(now=NOW, text='1430'))
    elif origin == 'publication':
        pub = services.publications.schedule(post, future(minutes=60), now_epoch=NOW)
        old_view = flow.pub_open(now=NOW, pub_id=pub.id)
    else:
        old_view = ask
    label = texts.BTN_CANCEL if origin == 'confirmation' else texts.BTN_BACK
    old = find_token(old_view, label)
    if origin == 'publication':
        press_label(flow, old_view, texts.BTN_EDIT_TIME, now=NOW)
    else:
        flow.draft_schedule_ask(now=NOW, post_id=post)
    confirm = run(flow.text(now=NOW, text='1530'))
    before = flow._ui()
    pubs = services.publications.nearest_planned()
    stale = press(flow, old, now=NOW)
    assert stale.stale_callback and flow._ui() == before
    assert services.publications.nearest_planned() == pubs
    assert texts.BTN_CONFIRM in labels(confirm)
    assert press(flow, old, now=NOW).stale_callback


@pytest.mark.parametrize('screen', ['picker', 'confirmation', 'reschedule'])
def test_current_scheduling_back_or_cancel_is_valid(services, screen):
    flow, post, card = picker(services)
    if screen == 'reschedule':
        pub = services.publications.schedule(post, future(minutes=60), now_epoch=NOW)
        view = flow.pub_open(now=NOW, pub_id=pub.id)
        press_label(flow, view, texts.BTN_EDIT_TIME, now=NOW)
    if screen != 'picker':
        card = run(flow.text(now=NOW, text='1530'))
    pubs = services.publications.nearest_planned()
    reply = press_label(flow, card, texts.BTN_BACK if screen == 'picker'
                        else texts.BTN_CANCEL, now=NOW)
    assert not reply.stale_callback and flow._ui().state is State.MENU
    assert services.publications.nearest_planned() == pubs


@pytest.mark.parametrize('button', ['picker_back', 'day_back', 'day_page', 'hour',
                                    'cancel', 'confirm', 'reschedule_cancel'])
def test_scheduling_capability_rechecked_after_acknowledgement(services, button):
    flow, post, card = picker(services)
    if button in ('day_back', 'day_page', 'hour'):
        card = press_label(flow, card, texts.BTN_DAY_SLOTS, now=NOW)
        label = {'day_back': texts.BTN_BACK, 'day_page': 'День →', 'hour': '12:00'}[button]
    elif button == 'picker_back':
        label = texts.BTN_BACK
    else:
        if button == 'reschedule_cancel':
            pub = services.publications.schedule(post, future(minutes=60), now_epoch=NOW)
            view = flow.pub_open(now=NOW, pub_id=pub.id)
            press_label(flow, view, texts.BTN_EDIT_TIME, now=NOW)
        card = run(flow.text(now=NOW, text='1430'))
        label = texts.BTN_CONFIRM if button == 'confirm' else texts.BTN_CANCEL
    token = find_token(card, label)
    before = []
    async def move_screen():
        run_reply = await flow.text(now=NOW, text='1530')
        before.append(flow._ui())
        assert '15:30' in run_reply.text
    pubs = services.publications.nearest_planned()
    stale = run(flow.press(now=NOW, token=token, on_consumed=move_screen))
    assert stale.stale_callback and flow._ui() == before[0]
    assert services.publications.nearest_planned() == pubs


def test_dispatcher_stale_picker_back_keeps_confirmation_and_preview(services):
    from test_ui_handlers_offline import _Harness, _raw_message, OWNER
    h = _Harness(services)
    h.message(text='/start')
    h.press_text(texts.MENU_NEW_POST)
    h.message(text='post', message_id=40)
    h.press_text(texts.BTN_CONTINUE)
    old = h._button_data(texts.BTN_BACK)
    h.message(text='1430', message_id=41)
    before = h.ui.get()
    visible = dict(h.session.visible)
    h.feed({'callback_query': {
        'id': 'old-picker-back',
        'from': {'id': OWNER, 'is_bot': False, 'first_name': 'owner'},
        'chat_instance': 'ci', 'data': old,
        'message': _raw_message(message_id=h.card_id(), text='card'),
    }})
    assert h.ui.get() == before and h.session.visible == visible
    assert texts.BTN_CONFIRM in h.last_labels()


def test_dashboard_day_navigation_cannot_race_into_new_picker(services):
    flow, post, _ = picker(services)
    dashboard = flow.plan_today(now=NOW)
    token = find_token(dashboard, 'След. →')
    before = []
    async def open_picker():
        flow.draft_schedule_ask(now=NOW, post_id=post)
        before.append(flow._ui())
    stale = run(flow.press(now=NOW, token=token, on_consumed=open_picker))
    assert stale.stale_callback and flow._ui() == before[0]
    assert flow._ui().arg('schedule_picker')
    assert services.publications.get_by_post(post) is None
