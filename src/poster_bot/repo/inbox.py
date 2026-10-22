"""Durable inbox for incoming updates (PLAN §6: backlog is never silently
dropped; mark-after-persist, not mark-before-handler).

Ordering contract for one update:

1. ``begin`` records the receipt *before* any handler runs (crash after this
   point means the update is re-delivered and reprocessed, never lost);
2. the handler persists all of the update's durable effects;
3. ``complete`` marks the update done *after* those effects are committed.

A crash between 2 and 3 redelivers the update; handler effects are idempotent
(state compare-and-set, unique constraints), so reprocessing is safe. The
resume offset (``safe_offset``) never advances past a pending row, so Telegram
can never prune an update this process has not durably finished.
"""
from __future__ import annotations

import sqlite3
from enum import StrEnum

from ..db import iso, utc_now


class InboxStatus(StrEnum):
    NEW = "new"    # first receipt: handle it
    REDO = "redo"  # seen before, not completed (crash): handle idempotently
    DONE = "done"  # effects durably persisted: skip (dedup drop)


def _now_iso(now_epoch: int | None) -> str:
    return iso(int(utc_now().timestamp()) if now_epoch is None else now_epoch)


class InboxRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def begin(self, update_id: int, *, now_epoch: int | None = None) -> InboxStatus:
        """Record the receipt; classify what the handler should do."""
        if update_id <= 0:
            raise ValueError("update_id must be positive")
        stamp = _now_iso(now_epoch)
        with self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO inbox_updates(update_id, received_at)"
                " VALUES (?, ?)",
                (update_id, stamp),
            )
        if cur.rowcount == 1:
            return InboxStatus.NEW
        row = self._conn.execute(
            "SELECT done_at FROM inbox_updates WHERE update_id=?", (update_id,)
        ).fetchone()
        return InboxStatus.DONE if row["done_at"] is not None else InboxStatus.REDO

    def complete(self, update_id: int, *, now_epoch: int | None = None) -> None:
        """Effects are durable: mark done (idempotent)."""
        with self._conn:
            self._conn.execute(
                "UPDATE inbox_updates SET done_at=? WHERE update_id=?"
                " AND done_at IS NULL",
                (_now_iso(now_epoch), update_id),
            )

    def is_done(self, update_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM inbox_updates WHERE update_id=?"
            " AND done_at IS NOT NULL",
            (update_id,),
        ).fetchone()
        return row is not None

    def pending_ids(self) -> list[int]:
        """Received-but-not-finished updates (the durable backlog)."""
        rows = self._conn.execute(
            "SELECT update_id FROM inbox_updates WHERE done_at IS NULL"
            " ORDER BY update_id"
        ).fetchall()
        return [row["update_id"] for row in rows]

    def safe_offset(self) -> int | None:
        """getUpdates offset that discards nothing this process still owes:
        the smallest pending id, or the highest receipt plus one when the
        backlog is empty (None when nothing was ever received)."""
        row = self._conn.execute(
            "SELECT MIN(update_id) AS lo FROM inbox_updates"
            " WHERE done_at IS NULL"
        ).fetchone()
        if row["lo"] is not None:
            return int(row["lo"])
        row = self._conn.execute(
            "SELECT MAX(update_id) AS hi FROM inbox_updates"
        ).fetchone()
        return None if row["hi"] is None else int(row["hi"]) + 1

    def compact_completed_below_floor(self) -> int:
        """Delete DONE receipts strictly below the durable resume floor.

        Telegram returns ids >= the requested offset. Keep every pending
        row, DONE rows at/above that floor (for redelivery dedup), and the
        highest receipt as a durable high-water anchor. Without that anchor,
        an all-DONE table would be emptied and its next offset reset to None.
        The derived offset is identical before and after compaction, including
        after a restart. No schema or separate cursor is required.

        Returns the number of removed rows. Runs in one transaction so the
        floor read and the delete see the same table state.
        """
        with self._conn:
            floor = self.safe_offset()
            if floor is None:
                return 0
            cur = self._conn.execute(
                "DELETE FROM inbox_updates"
                " WHERE done_at IS NOT NULL AND update_id < ?"
                " AND update_id < (SELECT MAX(update_id) FROM inbox_updates)",
                (floor,),
            )
            return cur.rowcount
