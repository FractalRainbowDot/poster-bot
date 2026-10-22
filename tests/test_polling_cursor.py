"""Telegram's inclusive offset, real durable inbox and restart/maintenance."""
from __future__ import annotations

from polling_helpers import FakeSource, build_harness, run, update
from poster_bot.repo.inbox import InboxRepo


class TelegramQueue(FakeSource):
    """Confirm ids strictly BELOW offset; return ids >= offset."""

    def __init__(self, ids):
        super().__init__()
        self.queue = [update(i) for i in ids]

    async def fetch(self, *, offset, timeout):
        self.offsets.append(offset)
        if offset is not None:
            self.queue = [u for u in self.queue if u.update_id >= offset]
        return list(self.queue)


def test_completed_update_is_confirmed_not_repolled_forever(tmp_path):
    source = TelegramQueue([100])
    h = build_harness(tmp_path, source=source)
    try:
        reports = [run(h.loop.run_cycle()) for _ in range(5)]
        assert [r.fetched for r in reports] == [1, 0, 0, 0, 0]
        assert source.offsets == [None, 101, 101, 101, 101]
        assert h.handler.seen == [100]
        assert all(r.skipped_done == 0 for r in reports)
    finally:
        h.app.close()


def test_pending_oldest_survives_compaction_restart_then_advances(tmp_path):
    source = TelegramQueue([99, 100, 101])
    h = build_harness(tmp_path, source=source)
    failures = {100}

    def fail_pending(item):
        if item.update_id in failures:
            raise RuntimeError("offline failure")

    h.handler.on_handle = fail_pending
    first = run(h.loop.run_cycle())
    assert first.pending_after == (100,)
    assert h.inbox.safe_offset() == 100
    assert h.inbox.compact_completed_below_floor() == 1
    h.app.close()

    # Reopen the on-disk database: no in-memory cursor can hide a regression.
    restarted = build_harness(tmp_path, source=source, on_handle=fail_pending)
    try:
        again = run(restarted.loop.run_cycle())
        assert source.offsets[-1] == 100
        assert again.pending_after == (100,)
        assert again.skipped_done == 1  # newer DONE id is still deduplicated
        failures.clear()
        completed = run(restarted.loop.run_cycle())
        assert completed.handled == 1 and completed.pending_after == ()
        assert restarted.inbox.safe_offset() == 102
        restarted.inbox.compact_completed_below_floor()
        assert InboxRepo(restarted.app.conn).safe_offset() == 102
        assert restarted.inbox.is_done(101)  # persistent high-water anchor
        assert run(restarted.loop.run_cycle()).fetched == 0
        assert source.offsets[-1] == 102
    finally:
        restarted.app.close()
