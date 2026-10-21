"""Runtime ticks with a fake clock and fake transport: recovery end-to-end.

Covers the outage story: downtime stops sends (nothing ticks at all), the
restart sweep parks everything that fell due meanwhile, one persisted notice
drains from the outbox (at-least-once), and replays of a tick send nothing
again. Also covers the Russian notice text and its safe chunking under the
Telegram message limit.
"""

from __future__ import annotations

import asyncio

from conftest import NOW, Instant, text_content
from fake_transport import FakeTransport
from poster_bot.db import iso
from poster_bot.repo.notifications import OwnerNoticeStore
from poster_bot.repo.runtime_state import RuntimeState
from poster_bot.runtime import (
    NOTICE_PART_MAX_CHARS,
    Runtime,
    notice_parts,
    notice_text,
)


def run(coro):
    return asyncio.run(coro)


class Rt:
    """Runtime-level bundle over the shared services fixture."""

    def __init__(self, services, transport):
        self.services = services
        self.notices = OwnerNoticeStore(services.conn, services.journal)
        self.runtime = RuntimeState(services.conn)
        self.transport = transport
        self.rt = Runtime(
            dispatch=services.dispatch,
            notices=self.notices,
            runtime_state=self.runtime,
            transport=transport,
        )


def make_rt(services):
    transport = FakeTransport()
    return Rt(services, transport), transport


def schedule(services, epoch: int):
    post_id = services.drafts.create(text_content(), now_epoch=NOW)
    return services.publications.schedule(post_id, Instant(epoch), now_epoch=NOW)


def status_of(services, pub_id: int) -> str:
    return services.conn.execute(
        "SELECT status FROM publications WHERE id=?", (pub_id,)
    ).fetchone()["status"]


def simulate_restart(services, *, down_at: int, up_at: int):
    """Exactly what open_for_run does on a restart after a real gap: sweep,
    one persisted downtime notice, heartbeat reset."""
    result = services.dispatch.recovery_sweep(now_epoch=up_at)
    affected = tuple(
        sorted(set(result.uncertain_publication_ids) | set(result.missed_publication_ids))
    )
    OwnerNoticeStore(services.conn, services.journal).queue_recovery_downtime(
        start_epoch=down_at, end_epoch=up_at, affected_ids=affected, now_epoch=up_at
    )


class TestHealthyTicks:
    def test_tick_sends_due_post_and_beats_heartbeat(self, services):
        rt, transport = make_rt(services)
        pub = schedule(services, NOW + 7200)
        report = run(rt.rt.tick(now_epoch=pub.scheduled_epoch + 1))
        assert [r.outcome for r in report.sent] == ["published"]
        assert rt.runtime.last_alive() == pub.scheduled_epoch + 1
        assert rt.transport.calls and rt.transport.calls[0].kind == "text"

    def test_replayed_tick_sends_nothing_again(self, services):
        rt, transport = make_rt(services)
        pub = schedule(services, NOW + 7200)
        run(rt.rt.tick(now_epoch=pub.scheduled_epoch + 5))
        report = run(rt.rt.tick(now_epoch=pub.scheduled_epoch + 5))
        assert report.sent == ()
        assert len(transport.calls) == 1  # the minute's post went out once

    def test_tick_before_due_is_idle(self, services):
        rt, transport = make_rt(services)
        schedule(services, NOW + 7200)
        report = run(rt.rt.tick(now_epoch=NOW))
        assert not report.anything_happened
        assert transport.calls == []
        assert rt.runtime.last_alive() == NOW  # heartbeat still advances


class TestOutageStory:
    def test_nothing_falls_through_an_outage(self, services):
        # a post due while the bot is down is parked 'missed' by the restart
        # sweep and no later tick ever sends it
        pub = schedule(services, NOW + 300)  # due during the downtime window
        rt, transport = make_rt(services)
        up_at = NOW + 3000
        rt.runtime.set_alive(now_epoch=NOW)
        simulate_restart(services, down_at=NOW, up_at=up_at)
        assert status_of(services, pub.id) == "missed"
        report = run(rt.rt.tick(now_epoch=up_at))
        assert report.sent == ()
        assert transport.calls == []  # channel sends stayed off throughout

    def test_recovery_notice_delivered_exactly_once(self, services):
        pub = schedule(services, NOW + 300)
        rt, transport = make_rt(services)
        up_at = NOW + 3000
        rt.runtime.set_alive(now_epoch=NOW)
        simulate_restart(services, down_at=NOW, up_at=up_at)
        pending_before = [n.id for n in rt.notices.pending()]
        report = run(rt.rt.tick(now_epoch=up_at))
        assert report.notices_delivered == tuple(pending_before)
        assert len(report.notices_delivered) == 1
        text = transport.notices[0]
        assert iso(NOW) in text and iso(up_at) in text  # the time interval
        assert str(pub.id) in text  # affected publication ids
        # drained: a second tick does not re-notify the owner
        report2 = run(rt.rt.tick(now_epoch=up_at + 1))
        assert report2.notices_delivered == ()
        assert len(transport.notices) == 1

    def test_failed_notice_delivery_stays_pending(self, services):
        rt, transport = make_rt(services)
        up_at = NOW + 3000
        rt.runtime.set_alive(now_epoch=NOW)
        simulate_restart(services, down_at=NOW, up_at=up_at)
        transport.notify_fails = "transport_error"
        report = run(rt.rt.tick(now_epoch=up_at))
        assert report.notices_delivered == ()
        assert len(report.notices_pending) == 1
        notice = rt.notices.pending()[0]
        assert notice.status == "pending"
        # healthy later tick delivers it; nothing was lost
        transport.notify_fails = None
        report2 = run(rt.rt.tick(now_epoch=up_at + 60))
        assert report2.notices_delivered == (notice.id,)
        row = services.conn.execute(
            "SELECT status FROM owner_notices WHERE id=?", (notice.id,)
        ).fetchone()
        assert row["status"] == "sent"

    def test_notice_text_carries_only_ids_and_intervals(self, services):
        pub = schedule(services, NOW + 300)  # falls due inside the downtime
        rt, _ = make_rt(services)
        rt.runtime.set_alive(now_epoch=NOW)
        simulate_restart(services, down_at=NOW, up_at=NOW + 3000)
        notice = rt.notices.pending()[0]
        text = notice_text(notice)
        # concise Russian notice: downtime interval + affected ids
        assert "простой" in text
        assert "Затронуты публикации" in text
        assert str(pub.id) in text
        assert iso(NOW) in text and iso(NOW + 3000) in text  # epochs, no payloads
        assert "(UTC)" in text  # explicit zone marker (ISO stamps carry +00:00)
        assert "token" not in text.lower()

    def test_empty_affected_ids_notice_states_none(self, services):
        rt, _ = make_rt(services)
        rt.notices.queue_recovery_downtime(
            start_epoch=NOW, end_epoch=NOW + 3000, affected_ids=(),
            now_epoch=NOW + 3000,
        )
        parts = notice_parts(rt.notices.pending()[0])
        assert len(parts) == 1
        assert "Затронутых публикаций нет" in parts[0]


class _HalfFailingTransport(FakeTransport):
    """First notification succeeds, every later one fails: simulates an
    adapter error landing mid-chunk of a multi-part notice."""

    def __init__(self) -> None:
        super().__init__()
        self._notify_budget = 1

    async def notify_owner(self, text: str) -> None:
        if self._notify_budget <= 0:
            from poster_bot.transport.ports import TransportCallError

            raise TransportCallError("transport_error")
        self._notify_budget -= 1
        await super().notify_owner(text)


class TestNoticeChunking:
    def _queue_long_notice(self, services, count: int):
        store = OwnerNoticeStore(services.conn, services.journal)
        store.queue_recovery_downtime(
            start_epoch=NOW,
            end_epoch=NOW + 3000,
            affected_ids=tuple(range(1, count + 1)),
            now_epoch=NOW + 3000,
        )
        return store.pending()[0]

    def test_long_id_list_is_split_under_the_message_limit(self, services):
        # enough ids that one message would blow past Telegram's 4096 cap
        notice = self._queue_long_notice(services, 2000)
        parts = notice_parts(notice)
        assert len(parts) > 1
        for part in parts:
            assert len(part) <= NOTICE_PART_MAX_CHARS <= 4096
        joined = "\n".join(parts)
        assert str(1) in joined and str(2000) in joined  # no id lost
        # continuation parts stay labelled so the owner sees it is one notice
        assert any("продолжение" in part for part in parts[1:])

    def test_multi_part_notice_counts_as_one_delivery(self, services):
        notice = self._queue_long_notice(services, 2000)
        rt, transport = make_rt(services)
        report = run(rt.rt.deliver_notices(now_epoch=NOW + 3001))
        assert report == (tuple([notice.id]), ())
        assert len(transport.notices) > 1  # several messages...
        assert rt.notices.pending() == []  # ...one notice, marked sent once

    def test_failure_mid_chunk_keeps_the_notice_pending(self, services):
        notice = self._queue_long_notice(services, 2000)
        transport = _HalfFailingTransport()
        rt = Rt(services, transport)
        delivered, pending = run(rt.rt.deliver_notices(now_epoch=NOW + 3001))
        assert delivered == ()
        assert pending == (notice.id,)
        assert notice.id in [n.id for n in rt.notices.pending()]
        assert len(transport.notices) == 1  # first part went out, second failed
        # a fully healthy drain re-sends every part and finally marks sent:
        # accepted at-least-once duplication, never a lost notice
        healthy = FakeTransport()
        rt2 = Rt(services, healthy)
        delivered2, pending2 = run(rt2.rt.deliver_notices(now_epoch=NOW + 3002))
        assert delivered2 == (notice.id,) and pending2 == ()
        assert rt2.notices.pending() == []


class TestPollingHooks:
    def test_record_poll_success_advances_poll_heartbeat(self, services):
        rt, _ = make_rt(services)
        rt.rt.record_poll_success(now_epoch=NOW + 5)
        assert rt.runtime.last_poll() == NOW + 5