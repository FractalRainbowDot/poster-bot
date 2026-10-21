"""Durable polling dedup: replay-proof processing and resume offset."""

from __future__ import annotations

from poster_bot.repo.processed_updates import ProcessedUpdatesRepo


def repo(services) -> ProcessedUpdatesRepo:
    return ProcessedUpdatesRepo(services.conn)


class TestDedup:
    def test_first_seen_wins_and_replays_are_dropped(self, services):
        r = repo(services)
        assert r.mark_processed(101) is True
        assert r.mark_processed(101) is False
        assert r.was_processed(101) is True
        assert r.was_processed(102) is False

    def test_resume_offset_survives_redelivery(self, services):
        # Telegram may redeliver older updates after a reconnect: the stored
        # highest id is the durable offset the next getUpdates resumes from,
        # and dedup drops every id already handled regardless of order.
        r = repo(services)
        for update_id in (7, 3, 11):
            assert r.mark_processed(update_id) is True
        assert r.highest_processed() == 11
        assert r.highest_processed() + 1 == 12  # resume offset formula
        assert r.mark_processed(3) is False  # redelivered old update dropped

    def test_empty_store_has_no_offset(self, services):
        assert repo(services).highest_processed() is None

    def test_prune_keeps_boundary_and_offset(self, services):
        r = repo(services)
        for update_id in range(1, 6):
            r.mark_processed(update_id)
        assert r.prune_below(4) == 3  # ids 1,2,3 gone
        assert r.was_processed(3) is False
        assert r.was_processed(4) is True
        assert r.highest_processed() == 5  # offset never regresses
        # the pruned horizon is in Telegram's past: replays start at >= 4