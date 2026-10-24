"""Real dispatcher/private transport regression checks; no external services."""
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
import pytest

from conftest import NOW
from test_ui_handlers_offline import _Harness, _FakeApiSession, OWNER
from ui_flow_helpers import build_flow, FLOW_CHANNEL
from poster_bot.domain.signature import SignatureConfig
from poster_bot.transport.aiogram_channel import AiogramChannelTransport
from poster_bot.ui import handlers, texts
from poster_bot.ui.dashboard_presentation import DashboardPresentation
from poster_bot.ui.state import UiStateRepo


class VisibleSession(_FakeApiSession):
    async def make_request(self, bot, method, timeout=None):
        self.payload = method.model_dump(mode='json', exclude_none=True,
                                         fallback=lambda obj: str(obj))
        self.method = method.__api_method__
        return await super().make_request(bot, method, timeout)

    def _raw(self):
        raw = super()._raw()
        if self.method.startswith('send'):
            self.visible[raw['message_id']] = dict(self.payload)
        return raw


class ReviewHarness(_Harness):
    def __init__(self, services):
        self.services = services
        self.session = VisibleSession()
        self.bot = Bot('42:TEST', session=self.session)
        signature = SignatureConfig('Main', 'https://example.org/main',
                                    'Partner', 'https://example.org/partner')
        self.flow, self.preview = build_flow(services, signature=signature)
        self.flow._preview = AiogramChannelTransport(
            self.bot, channel_id=FLOW_CHANNEL, owner_id=OWNER)
        self.dashboard = DashboardPresentation(self.flow, owner_id=OWNER)
        self.dispatcher = Dispatcher(storage=MemoryStorage())
        self.now = NOW
        self.dispatcher.include_router(handlers.create_ui_router(
            self.flow, owner_id=OWNER, clock=lambda: self.now, dashboard=self.dashboard))
        self.ui = UiStateRepo(services.conn)
        self._update_id = 0


def create_stack(h):
    h.message(text='/start')
    h.press_text(texts.MENU_NEW_POST)
    for i in range(2):
        h.message(photo=[dict(file_id=f'PHOTO{i}', file_unique_id=f'UNIQ{i}',
                              width=10, height=10)], message_id=30+i,
                  media_group_id='album')
    for i in range(5):
        h.message(text=(f'Paragraph{i} ' * 100), message_id=40+i)
    h.press_text(texts.BTN_CONTINUE)
    post_id = h.flow._ui().arg('post_id')
    return post_id, h.flow._plans.get(post_id).plan


def assert_stack(h, plan):
    expected, actual = [], []
    for part in plan.parts:
        content = part.content
        expected.append(('album', tuple(i.file_id for i in content.items))
                        if content.kind == 'album' else ('text', content.text))
    for payload in h.session.visible.values():
        if isinstance(payload.get('media'), list):
            item = ('album', tuple(i['media'] for i in payload['media']))
            if item in expected and item not in actual:
                actual.append(item)
        elif ('text', payload.get('text')) in expected:
            actual.append(('text', payload['text']))
    assert actual == expected
    active = h.flow.navigation_store(OWNER).lifecycle._load()['active']
    assert len(active) == sum(len(part.content.items) if part.content.kind == 'album'
                              else 1 for part in plan.parts)
    assert all(mid in h.session.visible for mid in active)
    assert max(active) < h.card_id()  # complete stack precedes fresh bottom controls
    assert all(p.get('chat_id', OWNER) == OWNER for _, p in h.session.requests)


@pytest.mark.parametrize('scheduled', [False, True])
def test_frozen_stack_visible_at_final_confirmation_and_scheduled_detail(services, scheduled):
    h = ReviewHarness(services)
    post_id, plan = create_stack(h)
    assert_stack(h, plan)
    h.message(text='15:37', message_id=60)
    if scheduled:
        h.press_text(texts.BTN_CONFIRM)
        assert services.publications.get_by_post(post_id).status.value == 'scheduled'
    assert_stack(h, plan)


def test_picker_offers_twelve_sorted_future_instants_without_next_free(services):
    h = ReviewHarness(services)
    create_stack(h)
    services.manual_times.record(15, 10, now_epoch=NOW-2)
    services.manual_times.record(20, 30, now_epoch=NOW-1)
    h.press_text(texts.BTN_DAY_SLOTS)
    h.press_text(texts.BTN_BACK)
    h.press_text(texts.BTN_SCHEDULE)
    labels = h.last_labels()
    choices = [label for label in labels if ' → ' in label]
    assert len(choices) >= 12
    from datetime import datetime
    instants = [datetime.strptime(label.split(' → ')[1], '%d.%m.%Y %H:%M')
                for label in choices]
    assert instants == sorted(instants)
    assert texts.BTN_NEXT_SLOT not in labels


@pytest.mark.parametrize('payload', [dict(text='wrong moment'),
    dict(video=dict(file_id='V', file_unique_id='UV', width=10, height=10, duration=1))])
def test_unrelated_owner_input_has_discard_not_capture_receipt(services, payload):
    h = ReviewHarness(services)
    h.message(text='/start')
    before = h.ui.get()
    receipt_at_delete = []
    h.session.before_delete = lambda mid: receipt_at_delete.append(
        (mid, h.flow.input_discarded(f'{OWNER}:{mid}')))
    h.message(message_id=77, **payload)
    assert 77 not in h.session.visible
    assert h.flow.input_discarded(f'{OWNER}:77')
    assert not h.flow.input_captured(f'{OWNER}:77')
    assert (77, True) in receipt_at_delete
    assert h.ui.get() == before
    h.message(message_id=77, **payload)
    assert h.ui.get() == before


@pytest.mark.parametrize('zone', ['Europe/Moscow', 'America/New_York'])
def test_narrow_window_cross_day_candidates_free_unique_and_manual_deduplicated(services, zone):
    from datetime import datetime, timezone
    from conftest import text_content
    from poster_bot.domain.time import Instant
    h = ReviewHarness(services)
    services.settings.change(timezone=zone, slot_start_hour=9, slot_end_hour=10)
    h.now = int(datetime(2026, 10, 21, 23, 30, tzinfo=timezone.utc).timestamp())
    # Every reserving state must be omitted, across several future days.
    initial = h.flow._schedule_candidates(h.now)
    blocked = {slot.epoch for slot, _ in initial[:5]}
    for (slot, _), status in zip(initial, ['scheduled', 'suspended', 'sending', 'uncertain', 'missed']):
        post = services.drafts.create(text_content(), now_epoch=h.now)
        pub = services.publications.schedule(post, Instant(slot.epoch), now_epoch=h.now)
        with services.conn:
            services.conn.execute('UPDATE publications SET status=? WHERE id=?', (status, pub.id))
    services.manual_times.record(9, 0, now_epoch=h.now-2)
    services.manual_times.record(23, 50, now_epoch=h.now-1)
    candidates = h.flow._schedule_candidates(h.now)
    epochs = [slot.epoch for slot, _ in candidates]
    assert len(epochs) >= 12 and epochs == sorted(set(epochs))
    assert not blocked.intersection(epochs)
    assert all(epoch > h.now for epoch in epochs)
    assert all(slot.local(zone).hour in (9, 10) for slot, manual in candidates if manual is None)
    assert any(manual == (23, 50) for _, manual in candidates)
    assert len({slot.local(zone).date() for slot, _ in candidates}) >= 6


@pytest.mark.parametrize('scheduled', [False, True])
def test_partial_preview_failure_retains_truth_and_tracks_every_acknowledged_id(services, monkeypatch, scheduled):
    from poster_bot.transport.ports import DefinitiveFailure
    h = ReviewHarness(services)
    post_id, plan = create_stack(h)
    if scheduled:
        h.message(text='15:37', message_id=60)
    lifecycle = h.flow.navigation_store(OWNER).lifecycle
    before = lifecycle._load()['active']
    # Force a fresh presentation (an earlier visible context), rather than
    # expecting an unchanged complete stack to be needlessly resent.
    lifecycle.rebind_active(lifecycle._load()['active_key'], 'previous-context', now=h.now)
    original = h.flow._preview.send_preview
    calls = []

    async def partial(content):
        calls.append(content)
        if len(calls) == 2:
            return DefinitiveFailure('api_error')
        return await original(content)

    monkeypatch.setattr(h.flow._preview, 'send_preview', partial)
    if scheduled:
        h.press_text(texts.BTN_CONFIRM)
    else:
        h.message(text='15:37', message_id=60)
    assert len(calls) == 2
    assert 'Не удалось показать весь пост' in h.last_text()
    data = h.flow.navigation_store(OWNER).lifecycle._load()
    assert data['candidate'] == []
    assert data['active'] == before
    if scheduled:
        assert services.publications.get_by_post(post_id).status.value == 'scheduled'
        assert texts.SCHEDULED_PREFIX in h.last_text()
    else:
        assert services.publications.get_by_post(post_id) is None
        assert texts.BTN_CONFIRM not in h.last_labels()
    assert all(p.get('chat_id', OWNER) == OWNER for _, p in h.session.requests)


def test_discard_receipt_failure_never_deletes_input(services, monkeypatch):
    h = ReviewHarness(services)
    h.message(text='/start')

    def fail(*args, **kwargs):
        raise RuntimeError('isolated receipt failure')

    monkeypatch.setattr(h.flow._state, 'record_effect', fail)
    with pytest.raises(RuntimeError, match='receipt failure'):
        h.message(text='unrelated', message_id=77)
    assert 77 in h.session.visible
    assert not h.flow.input_finished(f'{OWNER}:77')


def test_discarded_replay_cannot_become_new_draft_content(services):
    h = ReviewHarness(services)
    h.message(text='/start')
    h.message(text='unrelated', message_id=77)
    h.press_text(texts.MENU_NEW_POST)
    before = h.ui.get()
    h.message(text='unrelated', message_id=77)
    assert h.ui.get() == before
    assert not h.flow.input_captured(f'{OWNER}:77')


def test_confirmation_replay_does_not_reschedule_or_send_new_stack(services):
    from test_ui_navigation import callback
    h = ReviewHarness(services)
    post_id, plan = create_stack(h)
    h.press_text(next(label for label in h.last_labels() if ' → ' in label))
    card, token = h.card_id(), h._button_data(texts.BTN_CONFIRM)
    h.press_text(texts.BTN_CONFIRM)
    pub = services.publications.get_by_post(post_id)
    before = len(h.session.requests)
    callback(h, token, card)
    assert services.publications.get_by_post(post_id) == pub
    assert not any(method.startswith('send') for method, _ in h.session.requests[before:])
    assert_stack(h, plan)


def test_exhausted_bounded_horizon_is_reported_honestly(services, monkeypatch):
    from datetime import timedelta
    from poster_bot.domain.time import day_slots, HORIZON_DAYS
    from poster_bot.ui.times_input import local_day
    h = ReviewHarness(services)
    create_stack(h)
    settings = services.settings.get()
    today = local_day(NOW, settings.timezone)
    reserved = {slot.minute for offset in range(HORIZON_DAYS)
                for slot in day_slots(today + timedelta(days=offset), settings.timezone,
                                      settings.start_hour, settings.end_hour)}
    monkeypatch.setattr(h.flow._publications, 'reserved_minutes', lambda: reserved)
    h.press_text(texts.BTN_DAY_SLOTS)
    h.press_text(texts.BTN_BACK)
    h.press_text(texts.BTN_SCHEDULE)
    assert not any(' → ' in label for label in h.last_labels())
    assert 'Свободных часов в пределах поиска нет' in h.last_text()
    assert texts.BTN_OWN_TIME not in h.last_labels()
    h.message(text='2359', message_id=60)
    assert texts.BTN_CONFIRM in h.last_labels()
