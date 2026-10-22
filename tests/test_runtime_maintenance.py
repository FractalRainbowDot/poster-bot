"""Periodic maintenance wired through the REAL runtime tick path.

Journal retention (30 days) and inbox compaction must run inside the tick
the production composition (Runtime.for_app) drives, at the injected runtime
clock, AFTER sends, best-effort, and must never touch drafts or publication
history. Every test here goes through Runtime.tick -- never by calling
Journal.prune directly -- so the wiring itself is pinned.
"""

from __future__ import annotations

import asyncio
import sqlite3

from conftest import NOW, Instant, text_content
from fake_transport import FakeTransport

from poster_bot.app import open_offline
from poster_bot.config import ENV_DB_PATH, ENV_LOCK_PATH, load_config
from poster_bot.journal import RETENTION_DAYS
from poster_bot.repo.inbox import InboxRepo
from poster_bot.repo.notifications import OwnerNoticeStore
from poster_bot.repo.runtime_state import RuntimeState
from poster_bot.runtime import MAINTENANCE_INTERVAL_SECONDS, Runtime

DAY = 24 * 3600
STALE_AGE = (RETENTION_DAYS + 1) * DAY  # strictly past the retention cutoff
FRESH_AGE = (RETENTION_DAYS - 1) * DAY  # inside retention


def run(coro):
    return asyncio.run(coro)


class MaintRt:
    """Runtime exactly as for_app composes it: journal + inbox injected."""

    def __init__(self, services, transport):
        self.services = services
        self.notices = OwnerNoticeStore(services.conn, services.journal)
        self.runtime = RuntimeState(services.conn)
        self.inbox = InboxRepo(services.conn)
        self.transport = transport
        self.rt = Runtime(
            dispatch=services.dispatch,
            notices=self.notices,
            runtime_state=self.runtime,
            transport=transport,
            journal=services.journal,
            inbox=self.inbox,
        )


def make_rt(services):
    transport = FakeTransport()
    return MaintRt(services, transport), transport


def stale_count(services, *, before_epoch: int) -> int:
    cutoff = before_epoch - RETENTION_DAYS * DAY
    row = services.conn.execute(
        "SELECT COUNT(*) FROM journal_events WHERE ts_epoch < ?", (cutoff,)
    ).fetchone()
    return int(row[0])


class TestJournalRetentionInTick:
    def test_first_tick_prunes_only_events_past_retention(self, services):
        services.journal.record("draft_created", now_epoch=NOW - STALE_AGE)
        services.journal.record(
            "recovery_sweep",
            now_epoch=NOW - STALE_AGE - DAY,
            uncertain_count=2,
            missed_count=1,
        )
        services.journal.record(
            "scheduled", now_epoch=NOW - FRESH_AGE, minute_epoch=NOW - FRESH_AGE
        )
        rt, _ = make_rt(services)
        report = run(rt.rt.tick(now_epoch=NOW))
        assert report.journal_pruned == 2
        assert stale_count(services, before_epoch=NOW) == 0
        kinds_epochs = [
            (row["kind"], row["ts_epoch"]) for row in services.journal.entries(limit=50)
        ]
        assert ("scheduled", NOW - FRESH_AGE) in kinds_epochs  # fresh survives
        assert (
            "journal_pruned",
            NOW,
        ) in kinds_epochs  # pruned itself is journaled (allowlisted count only)

    def test_prune_never_touches_drafts_or_publication_history(self, services):
        # publish one post for real through a healthy tick (creates history)
        rt, transport = make_rt(services)
        post_id = services.drafts.create(text_content(), now_epoch=NOW)
        pub = services.publications.schedule(
            post_id, Instant(NOW + 3600), now_epoch=NOW
        )
        run(rt.rt.tick(now_epoch=NOW + 3601))  # publishes + first maintenance
        assert len(transport.calls) == 1  # the post really went out
        services.journal.record("draft_created", now_epoch=NOW + 3601 - STALE_AGE)
        posts_before = services.conn.execute(
            "SELECT COUNT(*) FROM posts"
        ).fetchone()[0]
        pubs_before = services.conn.execute(
            "SELECT COUNT(*) FROM publications"
        ).fetchone()[0]
        hist_before = services.conn.execute(
            "SELECT COUNT(*) FROM published_history"
        ).fetchone()[0]
        assert hist_before == 1  # real published history exists now

        report = run(rt.rt.tick(now_epoch=NOW + 3601 + DAY))
        assert report.journal_pruned == 1
        assert (
            services.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            == posts_before
        )
        assert (
            services.conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
            == pubs_before
        )
        assert (
            services.conn.execute(
                "SELECT COUNT(*) FROM published_history WHERE publication_id = ?",
                (pub.id,),
            ).fetchone()[0]
            == hist_before
        )
        assert len(transport.calls) == 1  # maintenance never re-sends anything

    def test_throttle_once_per_interval_in_runtime_time(self, services):
        services.journal.record("draft_created", now_epoch=NOW - STALE_AGE)
        rt, _ = make_rt(services)
        report = run(rt.rt.tick(now_epoch=NOW))
        assert report.journal_pruned == 1  # first tick runs maintenance
        services.journal.record("draft_created", now_epoch=NOW - STALE_AGE + 1)
        report2 = run(rt.rt.tick(now_epoch=NOW + 3600))
        assert report2.journal_pruned == 0  # interval not elapsed: no attempt
        assert stale_count(services, before_epoch=NOW + 3600) == 1
        report3 = run(rt.rt.tick(now_epoch=NOW + MAINTENANCE_INTERVAL_SECONDS))
        assert report3.journal_pruned == 1  # next attempt after the interval
        assert stale_count(services, before_epoch=NOW + MAINTENANCE_INTERVAL_SECONDS) == 0

    def test_clock_rewind_does_not_rearm_maintenance(self, services):
        rt, _ = make_rt(services)
        run(rt.rt.tick(now_epoch=NOW))  # first maintenance
        services.journal.record("draft_created", now_epoch=NOW - STALE_AGE - 10)
        report = run(rt.rt.tick(now_epoch=NOW - 10))  # rewound clock
        assert report.journal_pruned == 0  # monotonic guard: not due again

    def test_prune_failure_never_breaks_the_send_pipeline(self, services,
                                                          monkeypatch):
        post_id = services.drafts.create(text_content(), now_epoch=NOW)
        pub = services.publications.schedule(
            post_id, Instant(NOW + 3600), now_epoch=NOW
        )
        rt, transport = make_rt(services)

        def boom(*, now_epoch: int | None = None) -> int:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(services.journal, "prune", boom)
        report = run(rt.rt.tick(now_epoch=pub.scheduled_epoch + 1))
        assert [r.outcome for r in report.sent] == ["published"]  # sends intact
        assert report.journal_pruned == 0  # failure is swallowed, logged
        assert len(transport.calls) == 1


class TestInboxCompactionInTick:
    def test_completed_below_floor_compacted_pending_and_floor_survive(
        self, services
    ):
        rt, _ = make_rt(services)
        for uid in (10, 11, 12):
            rt.inbox.begin(uid, now_epoch=NOW)
        rt.inbox.complete(10, now_epoch=NOW)
        rt.inbox.complete(11, now_epoch=NOW)
        floor_before = rt.inbox.safe_offset()
        assert floor_before == 12  # inclusive min pending

        report = run(rt.rt.tick(now_epoch=NOW))
        assert report.inbox_compacted == 2  # DONE ids 10, 11 below floor
        assert rt.inbox.pending_ids() == [12]  # backlog untouched
        assert rt.inbox.safe_offset() == floor_before  # floor never regresses
        assert not rt.inbox.is_done(11)  # confirmed DONE receipt compacted
        assert not rt.inbox.is_done(12)  # pending stays pending

    def test_all_done_keeps_anchor_and_offset(self, services):
        rt, _ = make_rt(services)
        for uid in (5, 6):
            rt.inbox.begin(uid, now_epoch=NOW)
            rt.inbox.complete(uid, now_epoch=NOW)
        floor_before = rt.inbox.safe_offset()
        assert floor_before == 7  # highest receipt + 1

        report = run(rt.rt.tick(now_epoch=NOW))
        assert report.inbox_compacted == 1  # done id 5 dropped, anchor 6 kept
        assert rt.inbox.safe_offset() == floor_before  # resume offset stable
        kept = [
            row["update_id"]
            for row in services.conn.execute(
                "SELECT update_id FROM inbox_updates ORDER BY update_id"
            )
        ]
        assert kept == [6]
        # the anchor keeps deduping exactly as before compaction
        from poster_bot.repo.inbox import InboxStatus

        assert rt.inbox.begin(6, now_epoch=NOW) is InboxStatus.DONE
        # every compacted id is strictly below the resume floor, so the
        # offset every future fetch uses discards it server-side: it can
        # never return as a NEW receipt (no stale/replay regression)
        assert 5 < rt.inbox.safe_offset()

    def test_compaction_failure_never_breaks_the_send_pipeline(self, services,
                                                               monkeypatch):
        post_id = services.drafts.create(text_content(), now_epoch=NOW)
        pub = services.publications.schedule(
            post_id, Instant(NOW + 3600), now_epoch=NOW
        )
        rt, transport = make_rt(services)

        def boom() -> int:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(rt.inbox, "compact_completed_below_floor", boom)
        report = run(rt.rt.tick(now_epoch=pub.scheduled_epoch + 1))
        assert [r.outcome for r in report.sent] == ["published"]
        assert report.inbox_compacted == 0
        assert len(transport.calls) == 1


class TestMaintenanceGating:
    def test_uninjected_runtime_performs_no_maintenance(self, services):
        # exactly how the polling-test harness composes Runtime: no journal,
        # no inbox -> no maintenance at all (older construction sites inert)
        transport = FakeTransport()
        plain = Runtime(
            dispatch=services.dispatch,
            notices=OwnerNoticeStore(services.conn, services.journal),
            runtime_state=RuntimeState(services.conn),
            transport=transport,
        )
        services.journal.record("draft_created", now_epoch=NOW - STALE_AGE)
        report = run(plain.tick(now_epoch=NOW))
        assert report.journal_pruned == 0 and report.inbox_compacted == 0
        assert stale_count(services, before_epoch=NOW) == 1  # nothing removed

    def test_for_app_composition_runs_maintenance(self, tmp_path):
        config = load_config(
            {
                ENV_DB_PATH: str(tmp_path / "bot.sqlite3"),
                ENV_LOCK_PATH: str(tmp_path / "bot.lock"),
            }
        )
        app = open_offline(config)
        try:
            transport = FakeTransport()
            rt = Runtime.for_app(app, transport)
            app.journal.record("draft_created", now_epoch=NOW - STALE_AGE)
            inbox = InboxRepo(app.conn)
            inbox.begin(7, now_epoch=NOW)
            inbox.complete(7, now_epoch=NOW)
            inbox.begin(8, now_epoch=NOW)
            inbox.complete(8, now_epoch=NOW)
            run(rt.tick(now_epoch=NOW))
            row = app.conn.execute(
                "SELECT COUNT(*) FROM journal_events WHERE ts_epoch < ?",
                (NOW - RETENTION_DAYS * DAY,),
            ).fetchone()
            assert int(row[0]) == 0  # production wiring prunes for real
            assert [
                r["update_id"]
                for r in app.conn.execute(
                    "SELECT update_id FROM inbox_updates"
                )
            ] == [8]  # done 7 compacted, anchor 8 kept
            kinds = [r["kind"] for r in app.journal.entries(limit=20)]
            assert "journal_pruned" in kinds
        finally:
            app.close()
