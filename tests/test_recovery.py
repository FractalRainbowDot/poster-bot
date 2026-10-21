"""Recovery semantics: unfinished send -> uncertain (never auto-retried),
overdue -> missed (parked, manual replan only). Fake clock throughout."""
from __future__ import annotations

import pytest
from conftest import NOW, future, text_content
from poster_bot.domain.errors import (
    AttemptNotFound,
    MinuteTakenError,
    TransitionError,
)
from poster_bot.domain.states import Status
from poster_bot.domain.time import Instant
from poster_bot.repo.dispatch import DispatchService
from poster_bot.repo.drafts import DraftStore
from poster_bot.journal import Journal
from poster_bot.repo.publications import PublicationsService


def _reopen(services):
    """Simulate a process restart: brand-new connection and services."""
    db_path = services.conn.execute("PRAGMA database_list").fetchone()[2]
    services.conn.close()
    from poster_bot.db import connect

    conn = connect(db_path)
    return DispatchService(conn, Journal(conn)), conn


class TestUncertain:
    def test_crash_mid_send_becomes_uncertain(self, services, scheduled):
        pub, post_id = scheduled
        services.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch)
        fresh, conn = _reopen(services)
        result = fresh.recovery_sweep(now_epoch=pub.scheduled_epoch + 1)
        assert result.uncertain_publication_ids == (pub.id,)
        assert result.missed_publication_ids == ()
        status = PublicationsService(conn, Journal(conn)).get(pub.id).status
        assert status is Status.UNCERTAIN
        attempts = conn.execute(
            "SELECT state FROM send_attempts WHERE publication_id=?", (pub.id,)
        ).fetchall()
        assert [row["state"] for row in attempts] == ["abandoned"]
        assert len(fresh.unfinished_attempts()) == 0  # no retry queue

    def test_no_auto_retry_and_idempotent(self, services, scheduled):
        pub, _ = scheduled
        services.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch)
        services.dispatch.recovery_sweep(now_epoch=pub.scheduled_epoch + 1)
        again = services.dispatch.recovery_sweep(now_epoch=pub.scheduled_epoch + 2)
        assert not again.anything_changed
        attempts = services.conn.execute(
            "SELECT COUNT(*) c FROM send_attempts"
        ).fetchone()
        assert attempts["c"] == 1  # still exactly one: nothing was retried

    def test_uncertain_keeps_minute_reserved(self, services, scheduled):
        pub, _ = scheduled
        services.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch)
        services.dispatch.recovery_sweep(now_epoch=pub.scheduled_epoch + 1)
        other = services.drafts.create(text_content("x"), now_epoch=NOW)
        try:
            services.publications.schedule(other, future(), now_epoch=NOW + 1)
            raise AssertionError("minute must stay reserved while uncertain")
        except MinuteTakenError:
            pass

    def _crashed(self, services, scheduled):
        pub, _ = scheduled
        services.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch)
        services.dispatch.recovery_sweep(now_epoch=pub.scheduled_epoch + 1)
        return services.publications.get(pub.id)

    def test_owner_resolution_paths(self, services, scheduled):
        uncertain = self._crashed(services, scheduled)
        published = services.dispatch.resolve_uncertain_as_published(
            uncertain.id,
            uncertain.version,
            (77,),
            "https://t.me/c/9/77",
            now_epoch=uncertain.scheduled_epoch + 10,
        )
        assert published.status is Status.PUBLISHED

        pub2 = self._crashed(services, self._fresh(services))
        replanned = services.publications.replan(
            pub2.id, pub2.version, future(900), now_epoch=pub2.scheduled_epoch + 20
        )
        assert replanned.status is Status.SCHEDULED

    def _fresh(self, services):
        post_id = services.drafts.create(text_content("another"), now_epoch=NOW)
        pub = services.publications.schedule(post_id, future(700), now_epoch=NOW)
        return pub, post_id


class TestMissed:
    def test_overdue_becomes_missed_and_stays_parked(self, services, scheduled):
        pub, post_id = scheduled
        result = services.dispatch.recovery_sweep(now_epoch=future(1000).epoch)
        assert pub.id in result.missed_publication_ids
        current = services.publications.get(pub.id)
        assert current.status is Status.MISSED
        # parked: second sweep changes nothing
        assert not services.dispatch.recovery_sweep(
            now_epoch=future(1100).epoch
        ).anything_changed
        # no catch-up: the same slot will not fire automatically; owner acts
        assert services.dispatch.overdue_publications(
            now_epoch=future(1100).epoch
        ) == []

    def test_suspended_overdue_also_missed(self, services, scheduled):
        pub, _ = scheduled
        services.publications.pause(pub.id, pub.version, now_epoch=NOW)
        result = services.dispatch.recovery_sweep(now_epoch=future(1000).epoch)
        assert pub.id in result.missed_publication_ids

    def test_published_untouched_by_sweeps(self, services, scheduled):
        pub, _ = scheduled
        attempt = services.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch)
        services.dispatch.complete(
            pub.id, attempt, (5,), "https://t.me/c/1/5",
            now_epoch=pub.scheduled_epoch + 5,
        )
        result = services.dispatch.recovery_sweep(now_epoch=future(9000).epoch)
        assert not result.anything_changed
        assert services.publications.get(pub.id).status is Status.PUBLISHED

    def test_journal_records_sweep(self, services, scheduled):
        pub, _ = scheduled
        services.dispatch.recovery_sweep(now_epoch=future(1000).epoch)
        kinds = [row["kind"] for row in services.journal.entries(limit=50)]
        assert "marked_missed" in kinds and "recovery_sweep" in kinds


class TestConservativeSendOutcomes:
    """Live-process outcome recording: timeout/partial -> uncertain (never
    retried); definitive rejection -> missed (parked for manual replan, NOT
    back to scheduled, which would invite an automatic retry)."""

    def _in_flight(self, services, scheduled):
        pub, post_id = scheduled
        attempt = services.dispatch.claim(
            pub.id, pub.version, now_epoch=pub.scheduled_epoch
        )
        return pub, attempt

    def _kinds(self, services):
        return [row["kind"] for row in services.journal.entries(limit=50)]

    def test_timeout_parks_uncertain(self, services, scheduled):
        pub, attempt = self._in_flight(services, scheduled)
        updated = services.dispatch.mark_uncertain(
            pub.id, attempt, "timeout", now_epoch=pub.scheduled_epoch + 3
        )
        assert updated.status is Status.UNCERTAIN
        assert [
            row["state"]
            for row in services.conn.execute(
                "SELECT state FROM send_attempts WHERE publication_id=?", (pub.id,)
            )
        ] == ["abandoned"]
        assert "send_uncertain" in self._kinds(services)
        assert services.dispatch.unfinished_attempts() == []

    def test_partial_album_parks_uncertain(self, services, scheduled):
        pub, attempt = self._in_flight(services, scheduled)
        updated = services.dispatch.mark_uncertain(
            pub.id, attempt, "partial_result", now_epoch=pub.scheduled_epoch + 3
        )
        assert updated.status is Status.UNCERTAIN

    def test_non_allowlisted_code_is_refused_nothing_written(
        self, services, scheduled
    ):
        pub, attempt = self._in_flight(services, scheduled)
        before = len(services.journal.entries(limit=100))
        with pytest.raises(TransitionError):
            services.dispatch.mark_uncertain(
                pub.id, attempt, "secret-exception-text", now_epoch=pub.scheduled_epoch + 3
            )
        with pytest.raises(TransitionError):
            services.dispatch.fail_definitive(
                pub.id, attempt, "secret-exception-text", now_epoch=pub.scheduled_epoch + 3
            )
        assert services.publications.get(pub.id).status is Status.SENDING
        assert len(services.journal.entries(limit=100)) == before

    def test_attempt_must_belong_to_publication(self, services, scheduled):
        pub, _ = self._in_flight(services, scheduled)
        with pytest.raises(AttemptNotFound):
            services.dispatch.mark_uncertain(pub.id, 999, "timeout")

    def test_definitive_failure_parks_missed_then_manual_replan(
        self, services, scheduled
    ):
        pub, attempt = self._in_flight(services, scheduled)
        updated = services.dispatch.fail_definitive(
            pub.id, attempt, "api_error", now_epoch=pub.scheduled_epoch + 2
        )
        assert updated.status is Status.MISSED
        rows = services.conn.execute(
            "SELECT state, outcome FROM send_attempts WHERE publication_id=?",
            (pub.id,),
        ).fetchall()
        assert [(row["state"], row["outcome"]) for row in rows] == [
            ("finished", "definitive_failure")
        ]
        assert "send_failed_definitive" in self._kinds(services)
        replanned = services.publications.replan(
            pub.id,
            updated.version,
            future(900),
            now_epoch=pub.scheduled_epoch + 600,
        )
        assert replanned.status is Status.SCHEDULED


class TestUncertainAcknowledgedCancelToDraft:
    """An uncertain publication may only return to drafts through an explicit,
    acknowledged, version-checked owner action — never a silent replan that
    could duplicate unknown channel state."""

    def _uncertain(self, services, scheduled):
        pub, _ = scheduled
        services.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch)
        services.dispatch.recovery_sweep(now_epoch=pub.scheduled_epoch + 1)
        return services.publications.get(pub.id)

    def test_without_acknowledgement_forbidden(self, services, scheduled):
        uncertain = self._uncertain(services, scheduled)
        with pytest.raises(TransitionError):
            services.dispatch.resolve_uncertain_to_draft(
                uncertain.id, uncertain.version, acknowledged=False, now_epoch=NOW
            )
        assert services.publications.get(uncertain.id).status is Status.UNCERTAIN

    def test_stale_version_forbidden(self, services, scheduled):
        uncertain = self._uncertain(services, scheduled)
        with pytest.raises(TransitionError):
            services.dispatch.resolve_uncertain_to_draft(
                uncertain.id, uncertain.version - 1, acknowledged=True, now_epoch=NOW
            )

    def test_acknowledged_resolution_returns_post_to_drafts(
        self, services, scheduled
    ):
        pub, post_id = scheduled
        uncertain = self._uncertain(services, scheduled)
        services.dispatch.resolve_uncertain_to_draft(
            uncertain.id, uncertain.version, acknowledged=True, now_epoch=NOW + 10
        )
        # exactly one publication row went away; nothing new was planned
        assert services.conn.execute(
            "SELECT COUNT(*) c FROM publications"
        ).fetchone()["c"] == 0
        assert services.drafts.is_draft(post_id)
        # the minute is released for fresh scheduling
        fresh = services.publications.schedule(
            services.drafts.create(text_content("next"), now_epoch=NOW),
            Instant(pub.scheduled_epoch + 7200),
            now_epoch=NOW + 10,
        )
        assert fresh.status is Status.SCHEDULED
        kinds = [row["kind"] for row in services.journal.entries(limit=50)]
        assert "uncertain_cancelled_to_draft" in kinds