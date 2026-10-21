"""Durable update dedup for polling (used by the UI stage).

Telegram may redeliver updates after a reconnect. The processed set plus the
highest processed update id (the durable resume offset) let the future polling
loop discard replays *before* any handler runs, so reconnection can never
duplicate user-visible effects.
"""

from __future__ import annotations

import sqlite3

from ..db import iso, utc_now


class ProcessedUpdatesRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def mark_processed(self, update_id: int, *, now_epoch: int | None = None) -> bool:
        """Record an update id; True when this is the first time seeing it.

        INSERT OR IGNORE is the atomic test-and-set: two racing claimers of
        the same id cannot both see True."""
        now = int(utc_now().timestamp()) if now_epoch is None else now_epoch
        with self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO processed_updates(update_id, processed_at)"
                " VALUES (?, ?)",
                (update_id, iso(now)),
            )
        return cur.rowcount == 1

    def was_processed(self, update_id: int) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM processed_updates WHERE update_id=?", (update_id,)
        ).fetchone() is not None

    def highest_processed(self) -> int | None:
        """Durable resume offset: getUpdates offset = this + 1 on restart."""
        row = self._conn.execute(
            "SELECT MAX(update_id) AS hi FROM processed_updates"
        ).fetchone()
        return row["hi"] if row and row["hi"] is not None else None

    def prune_below(self, update_id: int) -> int:
        """Drop dedup history strictly below ``update_id`` (retention after
        Telegram's own redelivery horizon has passed); keeps the boundary id
        so the resume offset never regresses below a pruned replay window."""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM processed_updates WHERE update_id < ?", (update_id,)
            )
        return cur.rowcount