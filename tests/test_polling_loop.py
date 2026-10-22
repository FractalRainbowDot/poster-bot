"""Durable poll-loop contract (poster_bot.polling), fully offline.

Mark-before-handler / complete-after-effects, DONE dedup skips, offset
pinning by pending rows, no tick (hence no channel send) on failed polls,
connectivity-unsafe latching until a fresh poll, fresh clock after every
await, and idempotent REDO replay through the REAL owner flow: a crash
after the token-consuming mutation must neither duplicate the scheduled
publication nor drop the update.
"""
from __future__ import annotations

import re

from conftest import Instant, NOW, text_content
from polling_helpers import (
    FakeClock,
    advance_healthy,
    build_harness,
    run,
    update,
)
from ui_flow_helpers import find_token

from poster_bot.domain.states import Status
from poster_bot.polling import PollFailure, PollFatal
from poster_bot.transport.ports import UncertainOutcome
import poster_bot.ui.texts as texts

#: a scheduled minute far enough ahead to be strictly future at NOW; tests
#: advance the fake clock to its due time exactly like production would
DUE = NOW + 7200


def schedule_due(h):
    """A publication that is due at DUE (inside its minute window then)."""
    post_id = h.app.drafts.create(text_content(), now_epoch=NOW)
    pub = h.app.publications.schedule(post_id, Instant(DUE), now_epoch=NOW)
    return post_id, pub


class TestDurableOrdering:
    def test_begin_before_handler_complete_after(self, tmp_path):
        h = build_harness(tmp_path)
        observations = []

        def peek(_update):
            # during the handler the row exists but is NOT done
            observations.append(
                (h.inbox.is_done(_update.update_id),
                 _update.update_id in h.inbox.pending_ids())
            )

        h.handler.on_handle = peek
        h.source.script = [[update(11), update(12)]]
        report = run(h.loop.run_cycle())
        assert report.ok and report.fetched == 2 and report.handled == 2
        assert observations == [(False, True), (False, True)]
        assert h.inbox.is_done(11) and h.inbox.is_done(12)
        assert h.inbox.pending_ids() == []
        assert h.inbox.safe_offset() == 13

    def test_done_updates_are_skipped(self, tmp_path):
        h = build_harness(tmp_path)
        h.inbox.begin(5, now_epoch=NOW)
        h.inbox.complete(5, now_epoch=NOW)
        h.source.script = [[update(5)]]
        report = run(h.loop.run_cycle())
        assert report.skipped_done == 1 and report.handled == 0
        assert h.handler.seen == []

    def test_handler_crash_leaves_pending_then_redo_completes(self, tmp_path):
        crashes = {"first": True}

        def flaky(_update):
            if _update.update_id == 21 and crashes["first"]:
                crashes["first"] = False
                raise RuntimeError("simulated mid-handler crash")

        h = build_harness(tmp_path, on_handle=flaky)
        h.source.script = [[update(21), update(22)]]
        report = run(h.loop.run_cycle())
        assert report.handler_failures == 1
        # the crashed row stays pending (never done), the healthy one is done
        assert h.inbox.pending_ids() == [21]
        assert h.inbox.is_done(22)
        # Inclusive offset pins AT the pending id so Telegram re-delivers it.
        h.source.script = [[update(21)]]
        report2 = run(h.loop.run_cycle())
        assert h.source.offsets[-1] == 21  # min pending
        assert report2.handled == 1
        assert h.inbox.pending_ids() == []
        assert h.handler.seen.count(21) == 2  # REDO reprocessed

    def test_backlog_never_dropped_across_cycles(self, tmp_path):
        h = build_harness(tmp_path)
        h.source.script = [[update(10)]]
        run(h.loop.run_cycle())
        h.source.script = [[update(11), update(12)]]
        run(h.loop.run_cycle())
        assert h.source.offsets == [None, 11]  # durable offset progression
        assert h.inbox.safe_offset() == 13


class TestPollFailures:
    def test_failed_poll_has_no_tick_no_send_no_heartbeat(self, tmp_path):
        h = build_harness(tmp_path)
        post_id, pub = schedule_due(h)
        h.clock.t = pub.scheduled_epoch  # due: a healthy cycle would send
        h.source.script = [PollFailure("transport_error")]
        report = run(h.loop.run_cycle())
        assert not report.ok and report.failure_code == "transport_error"
        assert not report.ticked and h.ticks == []
        assert h.transport.calls == []  # no channel sends on poll failure
        assert h.app.runtime.last_poll() is None  # health NOT recorded
        assert report.sleep_seconds == 1.0
        # Network polling backoff doubles, capped at 4 seconds.
        for expected in (2.0, 4.0, 4.0):
            h.source.script = [PollFailure("transport_error")]
            assert run(h.loop.run_cycle()).sleep_seconds == expected
        # a fresh successful poll sends nothing new until health returns,
        # and the due-but-unclaimed post is parked by the reconnect sweep
        # (the outage just ended: its minute began inside the failure run)
        report = run(h.loop.run_cycle())
        assert report.reconnect and report.parked_missed == (pub.id,)
        assert h.transport.calls == []  # NO catch-up send for the missed minute
        assert h.app.publications.get(pub.id).status is Status.MISSED

    def test_backoff_cap_and_reset_after_success(self, tmp_path):
        h = build_harness(tmp_path)
        for expected in (1.0, 2.0, 4.0, 4.0, 4.0, 4.0, 4.0):
            h.source.script = [PollFailure("timeout")]
            assert run(h.loop.run_cycle()).sleep_seconds == expected
        report = run(h.loop.run_cycle())  # success
        assert report.ok and report.sleep_seconds == 0.0
        h.source.script = [PollFailure("timeout")]
        assert run(h.loop.run_cycle()).sleep_seconds == 1.0  # streak restarted

    def test_fatal_poll_propagates(self, tmp_path):
        h = build_harness(tmp_path)
        h.source.script = [PollFatal("unauthorized")]
        try:
            run(h.loop.run_cycle())
            raise AssertionError("PollFatal must propagate to the runner")
        except PollFatal as exc:
            assert exc.code == "unauthorized"

    def test_unclassified_source_crash_is_retryable(self, tmp_path):
        h = build_harness(tmp_path)
        h.source.script = [ValueError("adapter bug")]
        report = run(h.loop.run_cycle())
        assert not report.ok and report.failure_class == "ValueError"
        # the loop survived and polls again healthy
        assert run(h.loop.run_cycle()).ok


class TestConnectivityUnsafe:
    def test_uncertain_send_until_fresh_poll(self, tmp_path):
        h = build_harness(tmp_path)
        _, pub = schedule_due(h)
        advance_healthy(h, pub.scheduled_epoch - 1)
        h.clock.t = pub.scheduled_epoch
        h.transport.queue(UncertainOutcome(code="timeout"))
        report = run(h.loop.run_cycle())
        assert report.ticked and report.unsafe and h.loop.unsafe
        assert h.app.publications.get(pub.id).status is Status.UNCERTAIN
        # next cycle: a fresh successful poll clears the latch...
        report2 = run(h.loop.run_cycle())
        assert report2.ticked and not report2.unsafe
        # ...and the same publication is NEVER re-sent (no publish retry)
        assert len(h.transport.calls) == 1

    def test_tick_exception_sets_unsafe_until_fresh_poll(self, tmp_path):
        h = build_harness(tmp_path)
        h.loop._runtime.fail_next_tick = True
        report = run(h.loop.run_cycle())
        assert report.tick_failed and report.unsafe and h.loop.unsafe
        # any successful poll (which then ticks) re-proves connectivity
        report2 = run(h.loop.run_cycle())
        assert report2.ticked and not report2.unsafe

    def test_notice_delivery_failure_stays_pending_not_unsafe(self, tmp_path):
        # notice delivery is outbox-backed: pending, retried next tick —
        # it must NOT be conflated with an ambiguous channel publication
        h = build_harness(tmp_path)
        h.app.notices.queue_recovery_downtime(
            start_epoch=NOW - 300, end_epoch=NOW, affected_ids=(), now_epoch=NOW
        )
        h.transport.notify_fails = "transport_error"
        report = run(h.loop.run_cycle())
        assert report.ticked and not report.unsafe
        assert h.app.notices.pending() != []
        h.transport.notify_fails = None
        report = run(h.loop.run_cycle())
        assert report.ticked and h.app.notices.pending() == []


class TestFreshClock:
    def test_tick_uses_clock_refreshed_after_awaits(self, tmp_path):
        clock = FakeClock()
        h = build_harness(tmp_path, clock=clock)

        def consume_time(_update):
            # real time passes while the handler awaits Telegram/downloads
            clock.advance(30)

        h.handler.on_handle = consume_time
        h.source.script = [[update(7)]]
        run(h.loop.run_cycle())
        # poll health recorded at the pre-await NOW; the tick must use NOW+30
        assert h.app.runtime.last_poll() == NOW
        assert h.ticks == [NOW + 30]
        # inbox completion for the update also used the refreshed clock
        row = h.app.conn.execute(
            "SELECT done_at FROM inbox_updates WHERE update_id=7"
        ).fetchone()
        assert ":30+00:00" in row["done_at"]  # 08:00:30, not 08:00:00


class TestNoAutoCatchUpWhenHealthy:
    def test_exactly_due_post_publishes_in_window_once(self, tmp_path):
        h = build_harness(tmp_path)
        _, pub = schedule_due(h)
        advance_healthy(h, pub.scheduled_epoch)  # last step lands exactly at it
        run(h.loop.run_cycle())
        assert len(h.transport.calls) == 1
        # a later tick (window closed) must not touch it again
        h.clock.advance(61)
        run(h.loop.run_cycle())
        assert len(h.transport.calls) == 1


class TestIdempotentRedoWithRealFlow:
    def test_crash_after_scheduling_replay_schedules_exactly_once(self, tmp_path):
        """REDO replay through the production OwnerFlow: the confirm press
        atomically consumes the action token AND creates the publication.
        A crash right after that durable mutation re-delivers the update;
        the replayed press gets a stale-button reply and must not schedule
        a second publication."""
        h = build_harness(tmp_path)
        from poster_bot.ui.flow import OwnerFlow

        flow = OwnerFlow(
            conn=h.app.conn,
            settings=h.app.settings,
            drafts=h.app.drafts,
            publications=h.app.publications,
            dispatch=h.app.dispatch,
            manual_times=h.app.manual_times,
            journal=h.app.journal,
            preview=h.transport,
            channel_id=h.app.config.channel_id,
        )
        created = run(flow.new_post(now=NOW))
        staged = run(flow.text(now=NOW, text="replayed post"))
        created = run(_press(flow, staged, texts.BTN_CONTINUE))
        picker = run(_press(flow, created, texts.BTN_SCHEDULE))
        confirm = run(_press(flow, picker, texts.BTN_NEXT_SLOT))
        confirm = run(_press(flow, confirm, "12:00"))
        token = find_token(confirm, texts.BTN_CONFIRM)
        post_id = int(re.search(r"№(\d+)", created.text).group(1))

        crashes = {"first": True}

        class ConfirmHandler:
            async def handle(self, _update):
                reply = await flow.press(now=h.clock(), token=token)
                assert reply.text  # BUTTON_STALE reply on replay, never raises
                if crashes["first"]:
                    crashes["first"] = False
                    raise RuntimeError("crash after the durable mutation")

        h.loop._handler = ConfirmHandler()
        h.source.script = [[update(100)]]
        report = run(h.loop.run_cycle())
        assert report.handler_failures == 1
        assert h.inbox.pending_ids() == [100]
        assert h.app.publications.get_by_post(post_id).status is Status.SCHEDULED

        # redelivery at the pinned offset: replay must not duplicate
        h.source.script = [[update(100)]]
        report2 = run(h.loop.run_cycle())
        assert report2.handled == 1 and not report2.handler_failures
        assert h.inbox.is_done(100)
        assert h.app.publications.get_by_post(post_id).status is Status.SCHEDULED
        assert h.app.conn.execute(
            "SELECT COUNT(*) FROM publications WHERE post_id=?", (post_id,)
        ).fetchone()[0] == 1  # exactly one scheduled publication


async def _press(flow, reply, needle: str):
    return await flow.press(now=NOW, token=find_token(reply, needle))
