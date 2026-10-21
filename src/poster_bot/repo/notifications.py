"""Owner-notice outbox (PLAN §2: after reconnection exactly one owner notice
with downtime boundaries and affected posts; no other self-initiated owner
monitoring).

Reliability contract:
- a recovery notice is PERSISTED (at startup / on reconnect) before any
  delivery attempt, so a crash after the API call but before recording can
  never lose the notice — at-least-once delivery with pending/sent status;
- one notice per outage: UNIQUE(kind, downtime_start_epoch) dedups the same
  outage across restarts; re-opening inside the same outage merges (extends
  the end bound and unions affected ids) instead of queueing a second notice;
- affected ids are publication ids only; the notice never carries content.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..db import iso, utc_now
from ..domain.errors import DomainError
from ..journal import Journal
from .publications import immediate_tx

RECOVERY_KIND = "recovery"


class NoticeNotFound(DomainError):
    pass


@dataclass(frozen=True)
class Notice:
    id: int
    kind: str
    downtime_start_epoch: int
    downtime_end_epoch: int
    affected_ids: tuple[int, ...]
    status: str  # pending | sent
    created_at: str
    sent_at: str | None


def _epoch(now_epoch: int | None) -> int:
    return int(utc_now().timestamp()) if now_epoch is None else now_epoch


def _row_to_notice(row: sqlite3.Row) -> Notice:
    return Notice(
        id=row["id"],
        kind=row["kind"],
        downtime_start_epoch=row["downtime_start_epoch"],
        downtime_end_epoch=row["downtime_end_epoch"],
        affected_ids=tuple(json.loads(row["affected_ids"])),
        status=row["status"],
        created_at=row["created_at"],
        sent_at=row["sent_at"],
    )


class OwnerNoticeStore:
    def __init__(self, conn: sqlite3.Connection, journal: Journal) -> None:
        self._conn = conn
        self._journal = journal

    def queue_recovery_downtime(
        self,
        *,
        start_epoch: int,
        end_epoch: int,
        affected_ids: tuple[int, ...],
        now_epoch: int | None = None,
    ) -> Notice:
        """Persist (or merge into) the pending notice for this outage window.

        Returns the stored notice. Journaling ('notice_queued') happens only
        when the notice is new or its pending content actually changed."""
        if start_epoch >= end_epoch:
            raise DomainError("downtime window must advance: start < end")
        now = _epoch(now_epoch)
        stamp = iso(now)
        affected = tuple(sorted({int(a) for a in affected_ids}))
        changed = False
        with immediate_tx(self._conn) as conn:
            inserted = conn.execute(
                "INSERT INTO owner_notices(kind, downtime_start_epoch,"
                " downtime_end_epoch, affected_ids, status, created_at)"
                " VALUES (?, ?, ?, ?, 'pending', ?)"
                " ON CONFLICT(kind, downtime_start_epoch) DO NOTHING",
                (RECOVERY_KIND, start_epoch, end_epoch, json.dumps(affected), stamp),
            )
            changed = inserted.rowcount == 1
            row = conn.execute(
                "SELECT * FROM owner_notices WHERE kind=? AND"
                " downtime_start_epoch=?",
                (RECOVERY_KIND, start_epoch),
            ).fetchone()
            notice = _row_to_notice(row)
            if (
                not changed
                and notice.status == "pending"
                and (
                    end_epoch > notice.downtime_end_epoch
                    or not set(affected) <= set(notice.affected_ids)
                )
            ):
                merged_ids = tuple(sorted(set(notice.affected_ids) | set(affected)))
                merged_end = max(end_epoch, notice.downtime_end_epoch)
                conn.execute(
                    "UPDATE owner_notices SET downtime_end_epoch=?, affected_ids=?"
                    " WHERE id=? AND status='pending'",
                    (merged_end, json.dumps(merged_ids), notice.id),
                )
                notice = Notice(
                    id=notice.id,
                    kind=notice.kind,
                    downtime_start_epoch=notice.downtime_start_epoch,
                    downtime_end_epoch=merged_end,
                    affected_ids=merged_ids,
                    status=notice.status,
                    created_at=notice.created_at,
                    sent_at=None,
                )
                changed = True
        if changed:
            self._journal.record(
                "notice_queued",
                now_epoch=now,
                affected_count=len(notice.affected_ids),
            )
        return notice

    def pending(self) -> list[Notice]:
        rows = self._conn.execute(
            "SELECT * FROM owner_notices WHERE status='pending' ORDER BY id"
        ).fetchall()
        return [_row_to_notice(row) for row in rows]

    def mark_sent(self, notice_id: int, *, now_epoch: int | None = None) -> Notice:
        """Delivery confirmed by the transport: pending -> sent."""
        now = _epoch(now_epoch)
        stamp = iso(now)
        with self._conn:
            cur = self._conn.execute(
                "UPDATE owner_notices SET status='sent', sent_at=? WHERE id=?"
                " AND status='pending'",
                (stamp, notice_id),
            )
        if cur.rowcount != 1:
            raise NoticeNotFound(f"notice {notice_id} is not a pending notice")
        self._journal.record("notice_sent", now_epoch=now)
        row = self._conn.execute(
            "SELECT * FROM owner_notices WHERE id=?", (notice_id,)
        ).fetchone()
        return _row_to_notice(row)