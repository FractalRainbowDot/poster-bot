"""Offline Dispatcher-to-persistence probes for manual late-night scheduling."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from conftest import text_content
from test_ui_handlers_offline import _Harness, OWNER
from poster_bot.domain.time import Instant
from poster_bot.ui import texts


MOSCOW = ZoneInfo('Europe/Moscow')
NOON = datetime(2026, 10, 24, 12, tzinfo=MOSCOW)
CASES = [(day, clock) for day in (0, 1)
         for clock in ('23:00', '23:30', '23:59', '00:00')
         if day or clock != '00:00']


@pytest.mark.parametrize('hours', [(0, 23), (9, 18)])
@pytest.mark.parametrize('day,clock', CASES)
@pytest.mark.parametrize('entry', ['new', 'saved', 'reschedule'])
def test_manual_late_minute_dispatcher_persists_exact_epoch(services, hours, day, clock, entry):
    h = _Harness(services)
    h.now = int(NOON.timestamp())
    services.settings.change(timezone='Europe/Moscow',
                             slot_start_hour=hours[0], slot_end_hour=hours[1])
    h.message(text='/start')
    before = None
    if entry == 'new':
        h.press_text(texts.MENU_NEW_POST)
        h.message(text='late-night probe', message_id=40)
        h.press_text(texts.BTN_CONTINUE)
        post = h.ui.get().arg('post_id')
    else:
        post = services.drafts.create(text_content('late-night probe'), now_epoch=h.now)
        if entry == 'reschedule':
            before = services.publications.schedule(post, Instant(h.now + 3600), now_epoch=h.now)
        h.press_text(texts.MENU_DRAFTS if before is None else texts.MENU_PLAN)
        h.press_text(next(label for label in h.last_labels()
                          if ('late-night probe' in label)))
        h.press_text(texts.BTN_SCHEDULE if before is None else texts.BTN_EDIT_TIME)
    h.message(text=clock, message_id=41)
    if day:
        h.press_text('Изменить дату')
        h.press_text('Завтра')
    expected = NOON.replace(hour=int(clock[:2]), minute=int(clock[3:])) + timedelta(days=day)
    assert texts.BTN_CONFIRM in h.last_labels(), h.last_text()
    assert h.last_text() == f'❗ Публикация будет запланирована на {clock} {expected:%d.%m.%Y} ❗'
    assert h.ui.get().arg('minute_epoch') == int(expected.timestamp())
    assert h.flow.input_captured(f'{OWNER}:41')
    assert services.publications.get_by_post(post) == before
    h.press_text(texts.BTN_CONFIRM)
    actual = services.publications.get_by_post(post)
    assert actual.minute_epoch == int(expected.timestamp()), h.last_text()
    assert actual.status.value == 'scheduled'
    if before:
        assert actual.id == before.id and actual.version > before.version


@pytest.mark.parametrize('hours', [(0, 23), (9, 18)])
@pytest.mark.parametrize('day', [0, 1])
def test_manual_2330_uses_day_selected_before_input(services, hours, day):
    h = _Harness(services)
    h.now = int(NOON.timestamp())
    services.settings.change(timezone='Europe/Moscow',
                             slot_start_hour=hours[0], slot_end_hour=hours[1])
    h.message(text='/start')
    h.press_text(texts.MENU_NEW_POST)
    h.message(text='selected day probe', message_id=40)
    h.press_text(texts.BTN_CONTINUE)
    post = h.ui.get().arg('post_id')
    h.press_text(texts.BTN_DAY_SLOTS)
    if day:
        h.press_text('День →')
    h.message(text='23:30', message_id=41)
    expected = int((NOON.replace(hour=23, minute=30) + timedelta(days=day)).timestamp())
    assert texts.BTN_CONFIRM in h.last_labels(), h.last_text()
    assert services.publications.get_by_post(post) is None
    h.press_text(texts.BTN_CONFIRM)
    assert services.publications.get_by_post(post).minute_epoch == expected


@pytest.mark.parametrize('hours', [(0, 23), (9, 18)])
@pytest.mark.parametrize('day,clock', CASES)
@pytest.mark.parametrize('mode', ['native', 'signed'])
@pytest.mark.parametrize('entry', ['new', 'saved', 'reschedule'])
def test_live_manual_late_minute_preserves_review_and_persists(tmp_path, hours, day, clock, mode, entry):
    import asyncio

    from test_live_composition import live_config, make_bot, CHANNEL
    from test_ui_forward_intake import Intake, ORIGIN
    from test_ui_original_forward import NativeSession, labels
    from poster_bot.live import compose_live
    from poster_bot.repo.plans import PlanStore
    from poster_bot.ui.state import UiStateRepo

    async def scenario():
        now = int(NOON.timestamp())
        session = NativeSession()
        live = await compose_live(live_config(tmp_path, slot_start_hour=hours[0],
                                             slot_end_hour=hours[1]),
                                  clock=lambda: now, bot=make_bot(session), now_epoch=now)
        h = Intake(live, session)
        try:
            await h.message(text=texts.MENU_NEW_POST, message_id=2)
            await h.message(text='late-night source', forward_origin=ORIGIN, message_id=40)
            await h.press(texts.BTN_FORWARD_AS_IS if mode == 'native' else texts.BTN_COPY_SIGNED)
            await h.press(texts.BTN_CONTINUE)
            post = UiStateRepo(live.app.conn).get().arg('post_id')
            frozen = PlanStore(live.app.conn).get(post)
            before = None
            if entry == 'reschedule':
                await h.message(text='15:00', message_id=42)
                await h.press(texts.BTN_CONFIRM)
                before = live.app.publications.get_by_post(post)
            if entry != 'new':
                await h.message(text=texts.MENU_DRAFTS if before is None else texts.MENU_PLAN,
                                message_id=43)
                # This isolated database has exactly one saved/scheduled post;
                # native cards intentionally use a generic source label.
                await h.press(labels(h)[0])
                await h.press(texts.BTN_SCHEDULE if before is None else texts.BTN_EDIT_TIME)
            await h.message(text=clock, message_id=44)
            if day:
                await h.press('Изменить дату')
                await h.press('Завтра')
            expected = NOON.replace(hour=int(clock[:2]), minute=int(clock[3:])) + timedelta(days=day)
            assert h.card()['text'] == f'❗ Публикация будет запланирована на {clock} {expected:%d.%m.%Y} ❗'
            assert texts.BTN_CONFIRM in labels(h)
            assert live.app.publications.get_by_post(post) == before
            # The real live preview lifecycle must have approved the whole
            # immutable native/signed plan before final persistence.
            from poster_bot.ui.navigation import NavigationStore
            active = NavigationStore(live.app.conn, OWNER).lifecycle._load()['active']
            assert active and all(mid in session.visible for mid in active)
            assert max(active) < NavigationStore(live.app.conn, OWNER).get(OWNER)
            await h.press(texts.BTN_CONFIRM)
            actual = live.app.publications.get_by_post(post)
            assert actual.minute_epoch == int(expected.timestamp()), h.card()['text']
            assert actual.status.value == 'scheduled'
            assert PlanStore(live.app.conn).get(post) == frozen
            if before:
                assert actual.id == before.id and actual.version > before.version
            assert all(p.get('chat_id') != CHANNEL for _, p in session.requests)
            if mode == 'native':
                assert not h.deleted(40)
        finally:
            await live.close()

    asyncio.run(scenario())
