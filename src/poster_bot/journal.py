"""Journal (Журнал): 30 days of owner-visible events.

Redaction contract: every event carries ids and allowlisted structured detail
only. Post content, tokens and free-form strings can never be stored here —
values must be int/bool, or a string from a fixed safe vocabulary.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from .db import iso, utc_now

RETENTION_DAYS = 30

#: kind -> allowed detail keys
ALLOWED_KINDS: dict[str, frozenset[str]] = {
    "draft_created": frozenset(),
    "draft_updated": frozenset(),
    "draft_deleted": frozenset(),
    "scheduled": frozenset({"minute_epoch"}),
    "edited_paused": frozenset(),
    "paused": frozenset(),
    "resumed": frozenset(),
    "cancelled_to_draft": frozenset(),
    "post_deleted": frozenset(),
    "claimed": frozenset(),
    "claim_denied": frozenset({"reason_code"}),
    "attempt_prepared": frozenset(),
    "attempt_finished": frozenset({"outcome"}),
    "attempt_abandoned": frozenset(),
    "published": frozenset({"message_count"}),
    "attempt_in_progress": frozenset(),
    "marked_uncertain": frozenset(),
    "send_uncertain": frozenset({"code"}),
    "send_failed_definitive": frozenset({"code"}),
    "marked_missed": frozenset(),
    "uncertain_cancelled_to_draft": frozenset(),
    "recovery_sweep": frozenset({"uncertain_count", "missed_count"}),
    "outage_detected": frozenset({"downtime_seconds"}),
    "poller_stopped": frozenset(),
    "notice_queued": frozenset({"affected_count"}),
    "notice_sent": frozenset(),
    "settings_changed": frozenset({"keys_changed"}),
    "journal_pruned": frozenset({"removed_count"}),
}

#: the only string values acceptable anywhere in detail
SAFE_STRINGS = frozenset(
    {
        # attempt outcomes
        "published",
        "definitive_failure",
        # claim denial reasons
        "in_flight",
        "not_scheduled",
        "too_early",
        "past_due",
        "version_stale",
        # transport failure codes (allowlisted classification only, never
        # exception text, payloads or tokens)
        "timeout",
        "transport_error",
        "partial_result",
        "api_error",
        "rate_limited",
        # settings keys
        "slot_start_hour",
        "slot_end_hour",
        "timezone",
    }
)

#: transport codes accepted in send_* journal details
SEND_CODES = frozenset(
    {"timeout", "transport_error", "partial_result", "api_error", "rate_limited"}
)


class JournalRedactionError(ValueError):
    """A journal write would have stored non-allowlisted data."""


def _check_value(value: object) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, str) and value in SAFE_STRINGS:
        return
    raise JournalRedactionError(
        f"journal detail value of type {type(value).__name__} is not allowed"
    )


class Journal:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(
        self,
        kind: str,
        *,
        post_id: int | None = None,
        publication_id: int | None = None,
        now_epoch: int | None = None,
        **detail: object,
    ) -> int:
        if kind not in ALLOWED_KINDS:
            raise JournalRedactionError(f"unknown journal kind {kind!r}")
        allowed = ALLOWED_KINDS[kind]
        for key in detail:
            if key not in allowed:
                raise JournalRedactionError(
                    f"journal kind {kind!r} does not allow detail key {key!r}"
                )
        for value in detail.values():
            _check_value(value)
        epoch = int(utc_now().timestamp()) if now_epoch is None else now_epoch
        # isolation_level=None: a bare execute autocommits outside a
        # transaction and joins the open one inside (e.g. immediate_tx).
        # Never wrap in `with conn:` here: that would commit an outer
        # explicit transaction early.
        cur = self._conn.execute(
            "INSERT INTO journal_events(ts_epoch, ts, kind, post_id,"
            " publication_id, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (
                epoch,
                iso(epoch),
                kind,
                post_id,
                publication_id,
                json.dumps(detail, sort_keys=True),
            ),
        )
        return int(cur.lastrowid)

    def entries(self, *, limit: int = 100) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM journal_events ORDER BY ts_epoch DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def total(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM journal_events"
        ).fetchone()
        return int(row[0])

    def entries_page(self, offset: int,
                     limit: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM journal_events ORDER BY ts_epoch DESC, id DESC"
            " LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

    def prune(self, *, now_epoch: int | None = None) -> int:
        """Delete events older than RETENTION_DAYS; returns removed count."""
        epoch = int(utc_now().timestamp()) if now_epoch is None else now_epoch
        cutoff = epoch - int(timedelta(days=RETENTION_DAYS).total_seconds())
        cur = self._conn.execute(
            "DELETE FROM journal_events WHERE ts_epoch < ?", (cutoff,)
        )
        return cur.rowcount