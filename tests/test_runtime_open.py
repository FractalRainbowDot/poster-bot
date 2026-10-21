"""open_for_run reliability + owner-notice outbox.

Live-shaped dummy config only (fake token string, owner/channel ids): this
code path never imports aiogram and never touches the network. The recovery
notice must be PERSISTED before any delivery, survive crashes, dedup per
outage, and a failed open must never leak the singleton lock or the DB.
"""
from __future__ import annotations

import pytest
from conftest import NOW, future, text_content

from poster_bot.app import OUTAGE_TOLERANCE_SECONDS, open_for_run
from poster_bot.config import Config
from poster_bot.domain.errors import DomainError
from poster_bot.domain.states import Status
from poster_bot.domain.time import Instant
from poster_bot.repo.drafts import DraftStore
from poster_bot.repo.notifications import NoticeNotFound, OwnerNoticeStore
from poster_bot.repo.publications import PublicationsService
from poster_bot.journal import Journal
from poster_bot.singleton import AlreadyRunningError, ProcessLock

DUMMY_LIVE_TOKEN = "DUMMY_TEST_TOKEN_NOT_REAL"


def live_config(tmp_path) -> Config:
    return Config(
        bot_token=DUMMY_LIVE_TOKEN,
        owner_user_id=111,
        channel_id=-100222,
        db_path=str(tmp_path / "bot.sqlite3"),
        lock_path=str(tmp_path / "bot.lock"),
        timezone="Europe/Moscow",
        slot_start_hour=9,
        slot_end_hour=22,
    )


class TestOpenForRunBasics:
    def test_first_open_is_clean_and_releases_lock(self, tmp_path):
        config = live_config(tmp_path)
        app = open_for_run(config, now_epoch=NOW)
        assert app.notices.pending() == []  # no heartbeat yet: no "outage"
        assert app.runtime.last_alive() == NOW
        app.close()
        ProcessLock(config.lock_path).acquire()  # lock was fully released

    def test_second_instance_refused(self, tmp_path):
        config = live_config(tmp_path)
        app = open_for_run(config, now_epoch=NOW)
        with pytest.raises(AlreadyRunningError):
            open_for_run(config, now_epoch=NOW + 1)
        app.close()

    def test_live_config_failure_releases_lock(self, tmp_path):
        config = live_config(tmp_path)
        bad = Config(
            **{**config.__dict__, "owner_user_id": 0, "channel_id": 0}
        )
        with pytest.raises(Exception):
            open_for_run(bad, now_epoch=NOW)
        # a refused open must not leave the lock held
        lock = ProcessLock(config.lock_path)
        lock.acquire()
        lock.release()

    def test_storage_failure_after_lock_releases_lock(self, tmp_path):
        config = live_config(tmp_path)
        # a non-database file at the DB path fails migration after the lock
        with open(config.db_path, "w", encoding="utf-8") as handle:
            handle.write("this is not a sqlite file")
        with pytest.raises(Exception):
            open_for_run(config, now_epoch=NOW)
        lock = ProcessLock(config.lock_path)
        lock.acquire()
        lock.release()


class TestOutageNoticePersistence:
    def test_outage_yields_one_durable_notice_with_bounds_and_ids(self, tmp_path):
        config = live_config(tmp_path)
        first = open_for_run(config, now_epoch=NOW)
        post_id = first.drafts.create(text_content(), now_epoch=NOW)
        pub = first.publications.schedule(post_id, future(10), now_epoch=NOW)
        first.close()  # heartbeat stays at NOW

        # process restarts hours later: slot passed, downtime detected
        late = NOW + 3600
        second = open_for_run(config, now_epoch=late)
        pending = second.notices.pending()
        assert len(pending) == 1
        notice = pending[0]
        assert notice.downtime_start_epoch == NOW
        assert notice.downtime_end_epoch == late
        assert notice.affected_ids == (pub.id,)
        assert second.publications.get(pub.id).status is Status.MISSED
        kinds = [row["kind"] for row in second.journal.entries(limit=30)]
        assert "notice_queued" in kinds
        second.close()

        # a restart inside tolerance adds no second notice and loses nothing:
        # the notice lives in storage, not in process memory
        third = open_for_run(config, now_epoch=late + OUTAGE_TOLERANCE_SECONDS // 2)
        assert third.notices.pending() == [notice]

        # delivery marks it sent exactly once; a second mark is refused
        sent = third.notices.mark_sent(notice.id, now_epoch=late + 10)
        assert sent.status == "sent"
        assert third.notices.pending() == []
        with pytest.raises(NoticeNotFound):
            third.notices.mark_sent(notice.id, now_epoch=late + 11)
        kinds = [row["kind"] for row in third.journal.entries(limit=30)]
        assert "notice_sent" in kinds
        third.close()

    def test_uncertain_from_crash_mid_send_is_also_affected(self, tmp_path):
        config = live_config(tmp_path)
        first = open_for_run(config, now_epoch=NOW)
        post_id = first.drafts.create(text_content(), now_epoch=NOW)
        pub = first.publications.schedule(post_id, future(10), now_epoch=NOW)
        # send claimed, then the process "dies" before recording anything
        first.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch)
        db_path = first.conn.execute("PRAGMA database_list").fetchone()[2]
        first.conn.close()
        # simulated crash: the kernel drops the flock when a process dies
        first.lock.release()

        late = NOW + 3600
        second = open_for_run(config, now_epoch=late)
        notice = second.notices.pending()[0]
        assert notice.affected_ids == (pub.id,)
        assert second.publications.get(pub.id).status is Status.UNCERTAIN
        second.close()


class TestStartupGapParking:
    """A short restart (within tolerance) must still park a publication
    whose minute began inside the gap — letting it live would be the
    forbidden automatic catch-up — and persist the notice for it."""

    def test_within_tolerance_gap_parks_same_minute_post_with_notice(
        self, tmp_path
    ):
        config = live_config(tmp_path)
        first = open_for_run(config, now_epoch=NOW + 535)  # first ever: no notice
        post_id = first.drafts.create(text_content(), now_epoch=NOW + 536)
        pub = first.publications.schedule(
            post_id, Instant(NOW + 600), now_epoch=NOW + 536
        )
        first.close()

        # restart at NOW+630: gap 95s < tolerance, but the minute NOW+600
        # began at NOW+600 — inside the outage — while its window (to NOW+660)
        # is still open: it must be parked, not carried over.
        second = open_for_run(config, now_epoch=NOW + 630)
        assert second.publications.get(pub.id).status is Status.MISSED
        pending = second.notices.pending()
        assert len(pending) == 1
        assert pending[0].downtime_start_epoch == NOW + 535
        assert pending[0].downtime_end_epoch == NOW + 630
        assert pending[0].affected_ids == (pub.id,)
        second.close()

    def test_routine_restart_keeps_future_minute_alive(self, tmp_path):
        config = live_config(tmp_path)
        first = open_for_run(config, now_epoch=NOW)
        post_id = first.drafts.create(text_content(), now_epoch=NOW)
        pub = first.publications.schedule(post_id, Instant(NOW + 120), now_epoch=NOW)
        first.close()
        second = open_for_run(config, now_epoch=NOW + 30)  # quick clean restart
        # scheduled minute is in the future at reopen: untouched, and the
        # window will be claimable by the live scheduler as normal
        assert second.publications.get(pub.id).status is Status.SCHEDULED
        assert second.notices.pending() == []
        second.close()


class TestNoticeStoreDedupAndMerge:
    @pytest.fixture
    def store(self, services) -> OwnerNoticeStore:
        return OwnerNoticeStore(services.conn, services.journal)

    def test_window_must_advance(self, store):
        with pytest.raises(DomainError):
            store.queue_recovery_downtime(
                start_epoch=NOW, end_epoch=NOW, affected_ids=()
            )

    def test_same_outage_dedups_and_merges_pending_content(self, store):
        first = store.queue_recovery_downtime(
            start_epoch=NOW, end_epoch=NOW + 600, affected_ids=(5,), now_epoch=NOW + 600
        )
        merged = store.queue_recovery_downtime(
            start_epoch=NOW,
            end_epoch=NOW + 900,
            affected_ids=(3,),
            now_epoch=NOW + 900,
        )
        assert merged.id == first.id
        assert merged.downtime_end_epoch == NOW + 900
        assert merged.affected_ids == (3, 5)
        assert len(store.pending()) == 1
        # smaller/equal re-queue after sending must not resurrect or rewrite
        store.mark_sent(first.id, now_epoch=NOW + 950)
        after = store.queue_recovery_downtime(
            start_epoch=NOW, end_epoch=NOW + 700, affected_ids=(9,), now_epoch=NOW + 960
        )
        assert after.status == "sent"
        assert after.affected_ids == (3, 5)  # sent notice is immutable
        assert store.pending() == []

    def test_journal_records_queued_only_on_change(self, services, store):
        store.queue_recovery_downtime(
            start_epoch=NOW, end_epoch=NOW + 600, affected_ids=(5,), now_epoch=NOW + 600
        )
        store.queue_recovery_downtime(
            start_epoch=NOW, end_epoch=NOW + 600, affected_ids=(5,), now_epoch=NOW + 610
        )
        queued = [
            row
            for row in services.conn.execute(
                "SELECT kind FROM journal_events WHERE kind='notice_queued'"
            )
        ]
        assert len(queued) == 1