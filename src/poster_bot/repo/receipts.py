"""Per-part send intents, receipts and navigation edits (CHUNK2).

Storage contract (approved brief):
- intent before send: dispatch.claim snapshots one 'pending' publication_parts
  row per planned part IN THE SAME TRANSACTION as the claim CAS. A crash after
  the claim but before any ack leaves durable 'pending' rows; the recovery
  sweep parks the publication as 'uncertain' and nothing is ever auto-retried,
  so a pending row next to a resolved attempt is durable evidence of the exact
  part whose outcome is unknown;
- receipt right after the ack: the WHOLE channel message id list survives
  verbatim (album order included) plus the part's permalink. Replaying the
  identical ack is idempotent; replaying a DIFFERENT one is refused — a
  recorded receipt can never change, move or be overwritten by a late ack on
  an uncertain/failed part (owner verifies those manually);
- part outcomes (uncertain/failed) are recorded while the attempt is still in
  flight, i.e. BEFORE the publication-level mark_uncertain/fail_definitive;
- navigation edits are their own durable attempts (navigation_edits): only a
  part with a 'confirmed' receipt, only on a 'published' publication; a
  normally finished attempt requires every part received first, while an
  owner-resolved publication (attempt abandoned, verified in channel manually)
  keeps its received parts editable — the deliberate escape hatch. A failed
  edit NEVER touches the receipt rows: published bodies are immutable.
  Target chat/message references for the transport come from the stored
  receipts — never from callback data.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from ..db import iso, utc_now
from ..domain.errors import ContentValidationError, DomainError, TransitionError
from ..domain.original_forward import OriginalForwardPlan, SendSnapshot, parse_send_snapshot
from .publications import immediate_tx

IN_FLIGHT_ATTEMPT_STATES = ("prepared", "in_progress")

#: Telegram serves every channel permalink over HTTPS only — an ack carrying
#: anything else is not a real channel link. The authority grammar mirrors the
#: domain signature rule: a bare host (optionally :port), so any userinfo
#: ('user:pass@') and any malformed/empty host fail structurally here, in the
#: storage gate, before a bogus link can become a durable receipt or a history
#: button. Error texts are fixed and never echo the offending value.
_HTTPS_PREFIX = "https://"
_AUTHORITY_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?")


class PartNotFound(DomainError):
    """No intent row for this (attempt, part) — a planned-send bookkeeping
    error, e.g. an ack arriving for a legacy planless attempt."""


def _epoch(now_epoch: int | None) -> int:
    return int(utc_now().timestamp()) if now_epoch is None else now_epoch


def _validate_ack(channel_message_ids: object, permalink: object) -> tuple[int, ...]:
    ids = tuple(channel_message_ids)  # type: ignore[arg-type]
    if not ids:
        raise TransitionError("receipt needs the channel message ids")
    for mid in ids:
        if isinstance(mid, bool) or not isinstance(mid, int) or mid <= 0:
            raise TransitionError("channel message ids must be positive ints")
    if not isinstance(permalink, str) or not permalink:
        raise TransitionError("receipt needs the part's channel permalink")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in permalink):
        raise TransitionError("receipt permalink must not contain whitespace")
    if not permalink.startswith(_HTTPS_PREFIX):
        raise TransitionError("receipt permalink must be an absolute https URL")
    authority = permalink[len(_HTTPS_PREFIX):].split("/", 1)[0]
    if not _AUTHORITY_RE.fullmatch(authority):
        raise TransitionError(
            "receipt permalink must include a valid host without credentials")
    return ids


@dataclass(frozen=True)
class PublicationPart:
    id: int
    attempt_id: int
    part_index: int
    part_count: int
    state: str
    snapshot: SendSnapshot | None
    channel_message_ids: tuple[int, ...] | None
    permalink: str | None
    created_at: str
    updated_at: str
    confirmed_at: str | None


@dataclass(frozen=True)
class NavEdit:
    attempt_id: int
    part_index: int
    state: str
    last_error: str | None
    created_at: str
    updated_at: str


def _row_to_part(row: sqlite3.Row) -> PublicationPart:
    snapshot: SendSnapshot | None = None
    if row["part_snapshot_json"] is not None:
        # strict re-parse: a tampered intent snapshot never reaches readers
        snapshot = parse_send_snapshot(row["part_snapshot_json"])
    ids: tuple[int, ...] | None = None
    if row["channel_message_ids"] is not None:
        parsed = json.loads(row["channel_message_ids"])
        if not isinstance(parsed, list) or any(
            not isinstance(v, int) or isinstance(v, bool) for v in parsed
        ):
            raise ContentValidationError(
                "stored receipt ids are malformed for part "
                f"{row['part_index']}")
        ids = tuple(parsed)
    return PublicationPart(
        id=row["id"],
        attempt_id=row["attempt_id"],
        part_index=row["part_index"],
        part_count=row["part_count"],
        state=row["state"],
        snapshot=snapshot,
        channel_message_ids=ids,
        permalink=row["permalink"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        confirmed_at=row["confirmed_at"],
    )


class PartReceiptStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ---- reads -------------------------------------------------------------
    def parts_of_attempt(self, attempt_id: int) -> list[PublicationPart]:
        rows = self._conn.execute(
            "SELECT * FROM publication_parts WHERE attempt_id=?"
            " ORDER BY part_index", (attempt_id,)
        ).fetchall()
        return [_row_to_part(row) for row in rows]

    def all_parts_confirmed(self, attempt_id: int) -> bool:
        row = self._conn.execute(
            "SELECT COUNT(*) total, SUM(state='confirmed') confirmed FROM"
            " publication_parts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        return row["total"] > 0 and row["confirmed"] == row["total"]

    def get_nav_edit(self, attempt_id: int, part_index: int) -> NavEdit | None:
        row = self._conn.execute(
            "SELECT n.*, p.attempt_id, p.part_index FROM navigation_edits n"
            " JOIN publication_parts p ON p.id = n.part_id"
            " WHERE p.attempt_id=? AND p.part_index=?",
            (attempt_id, part_index),
        ).fetchone()
        if row is None:
            return None
        return NavEdit(
            attempt_id=row["attempt_id"],
            part_index=row["part_index"],
            state=row["state"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ---- receipts -----------------------------------------------------------
    def record_confirmed(
        self,
        attempt_id: int,
        part_index: int,
        channel_message_ids: tuple[int, ...],
        permalink: str,
        *,
        now_epoch: int | None = None,
    ) -> PublicationPart:
        """Turn this part's intent row into a receipt. Idempotent for the
        identical ack (safe after restarts/redelivered callbacks); a different
        second ack is refused forever — the recorded receipt is the truth."""
        ids = _validate_ack(channel_message_ids, permalink)
        now = _epoch(now_epoch)
        stamp = iso(now)
        ids_json = json.dumps(list(ids))
        with immediate_tx(self._conn) as conn:
            row = self._require_part(conn, attempt_id, part_index)
            snapshot = _row_to_part(row).snapshot
            if isinstance(snapshot, OriginalForwardPlan) and (
                len(ids) != len(snapshot.source.message_ids)
                or len(set(ids)) != len(ids)
            ):
                raise TransitionError("native receipt needs exact distinct destination ids")
            if row["state"] == "confirmed":
                if row["permalink"] == permalink and \
                        json.loads(row["channel_message_ids"]) == list(ids):
                    return self.parts_of_attempt(attempt_id)[part_index - 1]
                raise TransitionError(
                    f"part {part_index} already has a different recorded"
                    " receipt; a receipt can never change")
            if row["state"] != "pending":
                raise TransitionError(
                    f"part {part_index} outcome is {row['state']}; a late ack"
                    " never overwrites a recorded uncertain/failed outcome")
            self._require_in_flight(conn, attempt_id)
            won = conn.execute(
                "UPDATE publication_parts SET state='confirmed',"
                " channel_message_ids=?, permalink=?, updated_at=?,"
                " confirmed_at=? WHERE id=? AND state='pending'",
                (ids_json, permalink, stamp, stamp, row["id"]),
            )
            if won.rowcount != 1:
                raise TransitionError(
                    f"receipt for part {part_index} lost a concurrent update")
        return self._part_by_id(row["id"])

    def record_uncertain(
        self, attempt_id: int, part_index: int,
        *, now_epoch: int | None = None,
    ) -> PublicationPart:
        return self._record_outcome(attempt_id, part_index, "uncertain",
                                    now_epoch)

    def record_failed(
        self, attempt_id: int, part_index: int,
        *, now_epoch: int | None = None,
    ) -> PublicationPart:
        return self._record_outcome(attempt_id, part_index, "failed",
                                    now_epoch)

    def _record_outcome(
        self, attempt_id: int, part_index: int, outcome: str,
        now_epoch: int | None,
    ) -> PublicationPart:
        now = _epoch(now_epoch)
        stamp = iso(now)
        with immediate_tx(self._conn) as conn:
            row = self._require_part(conn, attempt_id, part_index)
            if row["state"] == outcome:
                return self._part_by_id(row["id"])   # idempotent replay
            if row["state"] != "pending":
                raise TransitionError(
                    f"part {part_index} already recorded as {row['state']};"
                    " outcomes never move backwards or overwrite a receipt")
            self._require_in_flight(conn, attempt_id)
            won = conn.execute(
                "UPDATE publication_parts SET state=?, updated_at=?"
                " WHERE id=? AND state='pending'",
                (outcome, stamp, row["id"]),
            )
            if won.rowcount != 1:
                raise TransitionError(
                    f"outcome for part {part_index} lost a concurrent update")
        return self._part_by_id(row["id"])

    # ---- internals -----------------------------------------------------------
    @staticmethod
    def _require_part(conn: sqlite3.Connection, attempt_id: int,
                      part_index: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM publication_parts WHERE attempt_id=?"
            " AND part_index=?", (attempt_id, part_index)
        ).fetchone()
        if row is None:
            raise PartNotFound(
                f"no send intent recorded for attempt {attempt_id} part"
                f" {part_index} (planless or unknown attempt)")
        return row

    @staticmethod
    def _require_in_flight(conn: sqlite3.Connection, attempt_id: int) -> None:
        att = conn.execute(
            "SELECT 1 FROM send_attempts WHERE id=? AND state IN"
            " ('prepared','in_progress')", (attempt_id,)
        ).fetchone()
        if att is None:
            raise TransitionError(
                f"attempt {attempt_id} is already resolved; part outcomes"
                " only change while the attempt is in flight")

    def _part_by_id(self, part_id: int) -> PublicationPart:
        row = self._conn.execute(
            "SELECT * FROM publication_parts WHERE id=?", (part_id,)
        ).fetchone()
        assert row is not None
        return _row_to_part(row)
    # ---- navigation edits ----------------------------------------------------
    def begin_nav_edit(
        self, attempt_id: int, part_index: int,
        *, now_epoch: int | None = None,
    ) -> NavEdit:
        """Record the start of a navigation edit attempt for one RECEIVED
        part of a COMPLETED publication. Idempotent while pending (crash-safe
        retry of the same in-flight edit); after a confirmed or failed edit a
        new begin opens a fresh attempt on the same part."""
        now = _epoch(now_epoch)
        stamp = iso(now)
        with immediate_tx(self._conn) as conn:
            part = self._require_part(conn, attempt_id, part_index)
            if isinstance(_row_to_part(part).snapshot, OriginalForwardPlan):
                raise TransitionError("native forwards have no navigation edits")
            if part["state"] != "confirmed":
                raise TransitionError(
                    f"part {part_index} has no confirmed receipt; navigation"
                    " may only edit parts the channel acknowledged")
            gate = conn.execute(
                "SELECT a.state AS attempt_state, p.status AS pub_status"
                " FROM send_attempts a JOIN publications p"
                " ON p.id = a.publication_id WHERE a.id=?",
                (attempt_id,),
            ).fetchone()
            if gate is None or gate["pub_status"] != "published":
                raise TransitionError(
                    "navigation edits require a publication that completed"
                    " as published")
            if gate["attempt_state"] == "finished":
                total, confirmed = conn.execute(
                    "SELECT COUNT(*), SUM(state='confirmed') FROM"
                    " publication_parts WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()
                if confirmed != total:
                    raise TransitionError(
                        "every part of the attempt must carry a receipt"
                        " before navigation links are edited")
            elif gate["attempt_state"] != "abandoned":
                raise TransitionError(
                    "the attempt is still in flight; navigation edits start"
                    " only after the publication completed")
            # 'abandoned' + published = owner-resolved outcome: editing the
            # individually-received parts stays allowed (manual verification
            # replaced the missing receipts); nothing is ever re-sent here.
            existing = conn.execute(
                "SELECT * FROM navigation_edits WHERE part_id=?",
                (part["id"],),
            ).fetchone()
            if existing is not None and existing["state"] == "pending":
                return self.get_nav_edit(attempt_id, part_index)  # no-op
            conn.execute(
                "INSERT INTO navigation_edits(part_id, state, last_error,"
                " created_at, updated_at) VALUES (?, 'pending', NULL, ?, ?)"
                " ON CONFLICT(part_id) DO UPDATE SET state='pending',"
                " last_error=NULL, updated_at=excluded.updated_at",
                (part["id"], stamp, stamp),
            )
        edit = self.get_nav_edit(attempt_id, part_index)
        assert edit is not None
        return edit

    def confirm_nav_edit(
        self, attempt_id: int, part_index: int,
        *, now_epoch: int | None = None,
    ) -> NavEdit:
        """Ack recorded for the in-flight edit. Only a pending edit confirms;
        the transport ack must belong to an edit this bot durably began."""
        return self._transition_nav(attempt_id, part_index, "pending",
                                    "confirmed", None, now_epoch)

    def fail_nav_edit(
        self, attempt_id: int, part_index: int, code: str,
        *, now_epoch: int | None = None,
    ) -> NavEdit:
        """The edit attempt failed; the part's receipt rows stay untouched —
        published bodies are immutable, only this edit attempt moves."""
        if not isinstance(code, str) or not code or code != code.strip():
            raise TransitionError("navigation failure needs a clean code")
        return self._transition_nav(attempt_id, part_index, "pending",
                                    "failed", code, now_epoch)

    def _transition_nav(
        self, attempt_id: int, part_index: int, expect: str, target: str,
        last_error: str | None, now_epoch: int | None,
    ) -> NavEdit:
        now = _epoch(now_epoch)
        stamp = iso(now)
        with immediate_tx(self._conn) as conn:
            part = self._require_part(conn, attempt_id, part_index)
            nav = conn.execute(
                "SELECT * FROM navigation_edits WHERE part_id=?",
                (part["id"],),
            ).fetchone()
            if nav is None:
                raise PartNotFound(
                    f"no navigation edit was begun for attempt {attempt_id}"
                    f" part {part_index}")
            if nav["state"] != expect:
                raise TransitionError(
                    f"navigation edit is {nav['state']}; only a pending edit"
                    f" can be {target}ed")
            won = conn.execute(
                "UPDATE navigation_edits SET state=?, last_error=?,"
                " updated_at=? WHERE id=? AND state=?",
                (target, last_error, stamp, nav["id"], expect),
            )
            if won.rowcount != 1:
                raise TransitionError(
                    "navigation edit lost a concurrent update")
        edit = self.get_nav_edit(attempt_id, part_index)
        assert edit is not None
        return edit
