"""Runtime state: tiny durable key/value facts written by the runtime itself.

Used for outage detection: the scheduler/poller persists a heartbeat epoch
while healthy; a new process (or a reconnecting poller) compares the stored
heartbeat with the current time to derive downtime boundaries for the owner
recovery notice. Values are code-controlled integer epochs only.
"""
from __future__ import annotations

import sqlite3

from ..db import iso, utc_now

KEY_LAST_ALIVE = "last_alive_epoch"
KEY_LAST_POLL = "last_successful_poll_epoch"

#: known keys; anything else is a programming error, not data
_KNOWN_KEYS = frozenset({KEY_LAST_ALIVE, KEY_LAST_POLL})


def _epoch(now_epoch: int | None) -> int:
    return int(utc_now().timestamp()) if now_epoch is None else now_epoch


class RuntimeState:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_int(self, key: str) -> int | None:
        if key not in _KNOWN_KEYS:
            raise KeyError(f"unknown runtime state key {key!r}")
        row = self._conn.execute(
            "SELECT value FROM runtime_state WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        return int(row["value"])

    def set_int(self, key: str, value: int, *, now_epoch: int | None = None) -> None:
        if key not in _KNOWN_KEYS:
            raise KeyError(f"unknown runtime state key {key!r}")
        if value < 0:
            raise ValueError("runtime state epoch must be non-negative")
        stamp = iso(_epoch(now_epoch))
        with self._conn:
            self._conn.execute(
                "INSERT INTO runtime_state(key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at",
                (key, str(int(value)), stamp),
            )

    # heartbeat shorthand -----------------------------------------------------
    def last_alive(self) -> int | None:
        return self.get_int(KEY_LAST_ALIVE)

    def set_alive(self, *, now_epoch: int | None = None) -> int:
        now = _epoch(now_epoch)
        self.set_int(KEY_LAST_ALIVE, now, now_epoch=now)
        return now

    def last_poll(self) -> int | None:
        return self.get_int(KEY_LAST_POLL)

    def set_poll(self, *, now_epoch: int | None = None) -> int:
        now = _epoch(now_epoch)
        self.set_int(KEY_LAST_POLL, now, now_epoch=now)
        return now