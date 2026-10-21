"""Recent owner-entered wall-clock minutes (hh:mm) — a suggestion source
separate from hourly slots (PLAN §2). Only distinct values, newest first.
"""
from __future__ import annotations

import sqlite3

from ..db import iso, utc_now
from ..domain.errors import DomainError


class ManualTimeError(DomainError):
    pass


class ManualTimesRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(self, hh: int, mm: int, *, now_epoch: int | None = None) -> None:
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ManualTimeError("manual minute must be within 00:00..23:59")
        epoch = int(utc_now().timestamp()) if now_epoch is None else now_epoch
        with self._conn:
            self._conn.execute(
                "INSERT INTO manual_times(hh, mm, last_used_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(hh, mm) DO UPDATE SET last_used_at=excluded.last_used_at",
                (hh, mm, iso(epoch)),
            )

    def recent(self, *, limit: int = 10) -> list[tuple[int, int]]:
        rows = self._conn.execute(
            "SELECT hh, mm FROM manual_times ORDER BY last_used_at DESC, hh, mm"
            " LIMIT ?",
            (limit,),
        ).fetchall()
        return [(row["hh"], row["mm"]) for row in rows]