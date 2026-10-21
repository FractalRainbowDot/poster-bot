"""Due-window acceptance for claim (the atomic time guard): healthy dispatch
lives only inside [scheduled_epoch, scheduled_epoch + 60); earlier is refused
('too_early'), the next minute onward is refused ('past_due') and the sweep
marks it missed. No replay after a reconnection with a stale clock.
"""
from __future__ import annotations

import json

import pytest

from conftest import NOW, future, text_content
from poster_bot.db import connect
from poster_bot.domain.errors import PastTimeError, TransitionError
from poster_bot.domain.states import Status
from poster_bot.domain.time import Instant
from poster_bot.journal import Journal
from poster_bot.repo.dispatch import DISPATCH_WINDOW_SECONDS, DispatchService
from poster_bot.repo.publications import PublicationsService


def _reason_of(services, publication_id: int) -> str | None:
    rows = services.journal.entries(limit=20)
    for row in rows:
        if row["kind"] == "claim_denied" and row["publication_id"] == publication_id:
            return json.loads(row["detail"])["reason_code"]
    return None


class TestClaimWindow:
    def test_claim_long_before_due_is_rejected(self, services, scheduled):
        pub, _ = scheduled
        with pytest.raises(TransitionError):
            services.dispatch.claim(pub.id, pub.version, now_epoch=NOW)
        assert _reason_of(services, pub.id) == "too_early"
        assert services.publications.get(pub.id).status is Status.SCHEDULED
        assert (
            services.conn.execute(
                "SELECT COUNT(*) c FROM send_attempts"
            ).fetchone()["c"]
            == 0
        )  # rejected claims leave no attempt behind

    def test_claim_at_due_boundary_inclusive(self, services, scheduled):
        pub, _ = scheduled
        attempt = services.dispatch.claim(
            pub.id, pub.version, now_epoch=pub.scheduled_epoch
        )
        assert attempt >= 1
        assert services.publications.get(pub.id).status is Status.SENDING

    def test_claim_inside_window_last_second(self, services, scheduled):
        pub, _ = scheduled
        services.dispatch.claim(
            pub.id,
            pub.version,
            now_epoch=pub.scheduled_epoch + DISPATCH_WINDOW_SECONDS - 1,
        )
        assert services.publications.get(pub.id).status is Status.SENDING

    def test_claim_at_next_minute_is_past_due(self, services, scheduled):
        pub, _ = scheduled
        with pytest.raises(TransitionError):
            services.dispatch.claim(
                pub.id,
                pub.version,
                now_epoch=pub.scheduled_epoch + DISPATCH_WINDOW_SECONDS,
            )
        assert _reason_of(services, pub.id) == "past_due"
        assert services.publications.get(pub.id).status is Status.SCHEDULED

    def test_stale_version_inside_window_denied(self, services, scheduled):
        pub, _ = scheduled
        services.conn.execute(
            "UPDATE publications SET version=version+1 WHERE id=?", (pub.id,)
        )
        with pytest.raises(TransitionError):
            services.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch)
        assert _reason_of(services, pub.id) == "version_stale"


class TestDueReadsAndSweep:
    def test_due_read_only_inside_window(self, services, scheduled):
        pub, _ = scheduled
        due = pub.scheduled_epoch
        assert services.dispatch.due_publications(now_epoch=due - 1) == []
        inside = services.dispatch.due_publications(now_epoch=due)
        assert [p.id for p in inside] == [pub.id]
        inside = services.dispatch.due_publications(
            now_epoch=due + DISPATCH_WINDOW_SECONDS - 1
        )
        assert [p.id for p in inside] == [pub.id]
        assert (
            services.dispatch.due_publications(
                now_epoch=due + DISPATCH_WINDOW_SECONDS
            )
            == []
        )

    def test_sweep_keeps_publication_inside_window(self, services, scheduled):
        pub, _ = scheduled
        result = services.dispatch.recovery_sweep(
            now_epoch=pub.scheduled_epoch + DISPATCH_WINDOW_SECONDS - 1
        )
        assert not result.anything_changed
        assert services.publications.get(pub.id).status is Status.SCHEDULED

    def test_sweep_misses_exactly_at_next_minute(self, services, scheduled):
        pub, _ = scheduled
        result = services.dispatch.recovery_sweep(
            now_epoch=pub.scheduled_epoch + DISPATCH_WINDOW_SECONDS
        )
        assert pub.id in result.missed_publication_ids
        assert services.publications.get(pub.id).status is Status.MISSED


class TestNoReplayAfterReconnect:
    def test_crash_before_claim_then_late_clock_marks_missed_not_sent(
        self, services, scheduled
    ):
        pub, post_id = scheduled
        db_path = services.conn.execute("PRAGMA database_list").fetchone()[2]
        services.conn.close()
        # a "reconnected" process with a clock now well past the slot
        conn = connect(db_path)
        fresh = DispatchService(conn, Journal(conn))
        late = pub.scheduled_epoch + 3 * DISPATCH_WINDOW_SECONDS
        result = fresh.recovery_sweep(now_epoch=late)
        assert pub.id in result.missed_publication_ids
        assert fresh.due_publications(now_epoch=late) == []
        current = PublicationsService(conn, Journal(conn)).get(pub.id)
        assert current.status is Status.MISSED
        with pytest.raises(TransitionError):
            fresh.claim(current.id, current.version, now_epoch=late)
        # only manual replanning to a new strictly-future minute resurrects it
        with pytest.raises(PastTimeError):
            PublicationsService(conn, Journal(conn)).replan(
                current.id, current.version, future(0), now_epoch=late
            )
        later = PublicationsService(conn, Journal(conn)).replan(
            current.id, current.version, Instant(late + 3600), now_epoch=late
        )
        assert later.status is Status.SCHEDULED
        assert (
            conn.execute(
                "SELECT COUNT(*) c FROM send_attempts"
            ).fetchone()["c"]
            == 0
        )  # nothing was ever claimed or attempted
        conn.close()