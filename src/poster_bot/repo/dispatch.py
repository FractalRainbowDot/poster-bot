"""Dispatch: claim, attempts, completion, outcome recording, recovery.

Safety contract (PLAN §5):
- claim is compare-and-set scheduled->sending together with a persistent
  pre-call attempt row, in one transaction: exactly one winner. The claim
  atomically enforces the due window: it only succeeds while the clock is
  inside [scheduled_epoch, scheduled_epoch + DISPATCH_WINDOW_SECONDS) —
  before the slot ('too_early') or at/after the next minute ('past_due')
  it is rejected by the same UPDATE that would claim, so a stale or eager
  worker can never send outside its slot (no replay after reconnection:
  anything past its window is swept to 'missed' instead);
- nothing about a publication changes while it is sending;
- no automatic retry anywhere: an unfinished attempt found at recovery makes
  the publication 'uncertain' for manual owner verification; a transport
  timeout/error parks conservatively as 'uncertain'; a definitive API failure
  (message provably not sent) parks the publication as 'missed' awaiting a
  manual replan;
- overdue scheduled/suspended (slot fully passed, now >= scheduled + window)
  become 'missed' and stay parked until the owner replans them manually
  (no catch-up).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from ..db import iso, utc_now
from ..domain.content import Content
from ..domain.errors import (
    AttemptNotFound,
    PublicationNotFound,
    TransitionError,
)
from ..domain.states import Status
from ..journal import SEND_CODES, Journal
from .publications import Publication, immediate_tx, row_to_publication

UNFINISHED_ATTEMPT_STATES = ("prepared", "in_progress")

#: a claim is healthy only inside [scheduled_epoch, scheduled_epoch + this)
DISPATCH_WINDOW_SECONDS = 60


def _epoch(now_epoch: int | None) -> int:
    return int(utc_now().timestamp()) if now_epoch is None else now_epoch


@dataclass(frozen=True)
class RecoveryResult:
    uncertain_publication_ids: tuple[int, ...] = ()
    missed_publication_ids: tuple[int, ...] = ()

    @property
    def anything_changed(self) -> bool:
        return bool(self.uncertain_publication_ids or self.missed_publication_ids)


class DispatchService:
    def __init__(self, conn: sqlite3.Connection, journal: Journal) -> None:
        self._conn = conn
        self._journal = journal

    # ---- claim ------------------------------------------------------------
    def claim(
        self, pub_id: int, expected_version: int, *, now_epoch: int | None = None
    ) -> int:
        """Compare-and-set scheduled -> sending plus persistent pre-call
        attempt row in one transaction. Returns the attempt id.

        The due window is part of the compare-and-set itself: the UPDATE only
        matches while now is inside [scheduled_epoch, scheduled_epoch + 60),
        so eagerness and staleness are rejected atomically (a check outside
        the write could never prevent the race against the clock)."""
        now = _epoch(now_epoch)
        stamp = iso(now)
        denial: str | None = None
        post_id: int | None = None
        with immediate_tx(self._conn) as conn:
            won = conn.execute(
                "UPDATE publications SET status='sending', version=version+1,"
                " updated_at=? WHERE id=? AND version=? AND status='scheduled'"
                " AND scheduled_epoch <= ? AND scheduled_epoch + ? > ?",
                (stamp, pub_id, expected_version, now, DISPATCH_WINDOW_SECONDS, now),
            )
            if won.rowcount != 1:
                denial = self._claim_denial_reason(conn, pub_id, now)
            else:
                row = conn.execute(
                    "SELECT post_id FROM publications WHERE id=?", (pub_id,)
                ).fetchone()
                post_id = row["post_id"]
                cur = conn.execute(
                    "INSERT INTO send_attempts(publication_id, state, created_at)"
                    " VALUES (?, 'prepared', ?)",
                    (pub_id, stamp),
                )
                attempt_id = int(cur.lastrowid)
        if denial is not None:
            # recorded outside the rolled-back transaction
            self._journal.record(
                "claim_denied",
                publication_id=pub_id,
                now_epoch=now,
                reason_code=denial,
            )
            raise TransitionError(
                f"claim lost for publication {pub_id}: {denial}"
            )
        self._journal.record(
            "claimed",
            post_id=post_id,
            publication_id=pub_id,
            now_epoch=now,
        )
        return attempt_id

    @staticmethod
    def _claim_denial_reason(
        conn: sqlite3.Connection, pub_id: int, now: int
    ) -> str:
        row = conn.execute(
            "SELECT status, scheduled_epoch FROM publications WHERE id=?",
            (pub_id,),
        ).fetchone()
        if row is None:
            return "not_scheduled"
        if row["status"] == Status.SENDING.value:
            return "in_flight"
        if row["status"] != Status.SCHEDULED.value:
            return "not_scheduled"
        if now < row["scheduled_epoch"]:
            return "too_early"
        if now >= row["scheduled_epoch"] + DISPATCH_WINDOW_SECONDS:
            return "past_due"
        # scheduled and healthy window: only the version guard could have lost
        return "version_stale"

    def mark_in_progress(self, attempt_id: int, *, now_epoch: int | None = None) -> None:
        """Optional marker that the external call has actually begun."""
        now = _epoch(now_epoch)
        with self._conn:
            cur = self._conn.execute(
                "UPDATE send_attempts SET state='in_progress' WHERE id=?"
                " AND state='prepared'",
                (attempt_id,),
            )
        if cur.rowcount != 1:
            raise AttemptNotFound(f"attempt {attempt_id} not in prepared state")
        self._journal.record(
            "attempt_in_progress", now_epoch=now
        )

    # ---- completion ---------------------------------------------------------
    def complete(
        self,
        pub_id: int,
        attempt_id: int,
        channel_message_ids: tuple[int, ...],
        permalink: str,
        *,
        now_epoch: int | None = None,
    ) -> Publication:
        """Confirmed success: sending -> published, attempt finished, history
        link stored (lives until the post is deleted). One transaction."""
        now = _epoch(now_epoch)
        stamp = iso(now)
        if not channel_message_ids or any(m <= 0 for m in channel_message_ids):
            raise TransitionError("completion needs positive channel message ids")
        if not permalink:
            raise TransitionError("completion needs a channel permalink")
        with immediate_tx(self._conn) as conn:
            pub_row = conn.execute(
                "SELECT * FROM publications WHERE id=?", (pub_id,)
            ).fetchone()
            if pub_row is None:
                raise PublicationNotFound(f"publication {pub_id} not found")
            won = conn.execute(
                "UPDATE publications SET status='published', version=version+1,"
                " updated_at=? WHERE id=? AND status='sending'",
                (stamp, pub_id),
            )
            if won.rowcount != 1:
                raise TransitionError(
                    "complete requires a sending publication (compare-and-set lost"
                    " or recovery already ran)"
                )
            att = conn.execute(
                "UPDATE send_attempts SET state='finished', outcome='published',"
                " finished_at=? WHERE id=? AND publication_id=?"
                " AND state IN ('prepared','in_progress')",
                (stamp, attempt_id, pub_id),
            )
            if att.rowcount != 1:
                raise AttemptNotFound(
                    f"attempt {attempt_id} is not an unfinished attempt of"
                    f" publication {pub_id}"
                )
            conn.execute(
                "INSERT INTO published_history(publication_id, post_id,"
                " channel_message_ids, permalink, published_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    pub_id,
                    pub_row["post_id"],
                    json.dumps(sorted(channel_message_ids)),
                    permalink,
                    stamp,
                ),
            )
        self._journal.record(
            "attempt_finished",
            publication_id=pub_id,
            now_epoch=now,
            outcome="published",
        )
        self._journal.record(
            "published",
            post_id=pub_row["post_id"],
            publication_id=pub_id,
            now_epoch=now,
            message_count=len(channel_message_ids),
        )
        return row_to_publication(self._conn.execute(
            "SELECT * FROM publications WHERE id=?", (pub_id,)
        ).fetchone())

    # ---- uncertain outcome recording (live process) -------------------------
    def _require_sending_attempt(
        self, conn: sqlite3.Connection, pub_id: int, attempt_id: int
    ) -> None:
        att = conn.execute(
            "SELECT 1 FROM send_attempts WHERE id=? AND publication_id=?"
            " AND state IN ('prepared','in_progress')",
            (attempt_id, pub_id),
        ).fetchone()
        if att is None:
            raise AttemptNotFound(
                f"attempt {attempt_id} is not an unfinished attempt of"
                f" publication {pub_id}"
            )

    @staticmethod
    def _require_send_code(code: str) -> None:
        if code not in SEND_CODES:
            raise TransitionError(
                f"send outcome code {code!r} is not allowlisted"
            )

    def mark_uncertain(
        self,
        pub_id: int,
        attempt_id: int,
        code: str,
        *,
        now_epoch: int | None = None,
    ) -> Publication:
        """Transport could not confirm the outcome (timeout, connection loss,
        partial album): park conservatively as 'uncertain', abandon the
        attempt. Never auto-retried; owner verifies the channel manually."""
        self._require_send_code(code)
        now = _epoch(now_epoch)
        stamp = iso(now)
        with immediate_tx(self._conn) as conn:
            self._require_sending_attempt(conn, pub_id, attempt_id)
            won = conn.execute(
                "UPDATE publications SET status='uncertain', version=version+1,"
                " updated_at=? WHERE id=? AND status='sending'",
                (stamp, pub_id),
            )
            if won.rowcount != 1:
                raise TransitionError(
                    "uncertain marking requires a sending publication"
                )
            conn.execute(
                "UPDATE send_attempts SET state='abandoned', finished_at=?"
                " WHERE id=? AND state IN ('prepared','in_progress')",
                (stamp, attempt_id),
            )
            row = conn.execute(
                "SELECT post_id FROM publications WHERE id=?", (pub_id,)
            ).fetchone()
        self._journal.record(
            "send_uncertain", publication_id=pub_id, now_epoch=now, code=code
        )
        self._journal.record(
            "attempt_abandoned",
            publication_id=pub_id,
            post_id=row["post_id"],
            now_epoch=now,
        )
        self._journal.record(
            "marked_uncertain",
            publication_id=pub_id,
            post_id=row["post_id"],
            now_epoch=now,
        )
        return row_to_publication(self._conn.execute(
            "SELECT * FROM publications WHERE id=?", (pub_id,)
        ).fetchone())

    def fail_definitive(
        self,
        pub_id: int,
        attempt_id: int,
        code: str,
        *,
        now_epoch: int | None = None,
    ) -> Publication:
        """Telegram provably did not send the post (rejected request). The
        slot passed without the post leaving: park as 'missed' for a manual
        replan — returning it to 'scheduled' would invite an automatic retry,
        which is forbidden."""
        self._require_send_code(code)
        now = _epoch(now_epoch)
        stamp = iso(now)
        with immediate_tx(self._conn) as conn:
            self._require_sending_attempt(conn, pub_id, attempt_id)
            won = conn.execute(
                "UPDATE publications SET status='missed', version=version+1,"
                " updated_at=? WHERE id=? AND status='sending'",
                (stamp, pub_id),
            )
            if won.rowcount != 1:
                raise TransitionError(
                    "definitive failure requires a sending publication"
                )
            conn.execute(
                "UPDATE send_attempts SET state='finished',"
                " outcome='definitive_failure', finished_at=?"
                " WHERE id=? AND state IN ('prepared','in_progress')",
                (stamp, attempt_id),
            )
            row = conn.execute(
                "SELECT post_id FROM publications WHERE id=?", (pub_id,)
            ).fetchone()
        self._journal.record(
            "send_failed_definitive",
            publication_id=pub_id,
            now_epoch=now,
            code=code,
        )
        self._journal.record(
            "attempt_finished",
            publication_id=pub_id,
            now_epoch=now,
            outcome="definitive_failure",
        )
        self._journal.record(
            "marked_missed",
            publication_id=pub_id,
            post_id=row["post_id"],
            now_epoch=now,
        )
        return row_to_publication(self._conn.execute(
            "SELECT * FROM publications WHERE id=?", (pub_id,)
        ).fetchone())

    # ---- uncertain resolution (owner actions only) ----------------------------
    def resolve_uncertain_as_published(
        self,
        pub_id: int,
        expected_version: int,
        channel_message_ids: tuple[int, ...],
        permalink: str,
        *,
        now_epoch: int | None = None,
    ) -> Publication:
        """Owner verified the post in the channel manually (Bot API has no
        chat history): record it published, no automatic action involved."""
        now = _epoch(now_epoch)
        stamp = iso(now)
        if not channel_message_ids or not permalink:
            raise TransitionError("resolution needs message ids and a permalink")
        with immediate_tx(self._conn) as conn:
            pub_row = conn.execute(
                "SELECT post_id FROM publications WHERE id=?", (pub_id,)
            ).fetchone()
            if pub_row is None:
                raise PublicationNotFound(f"publication {pub_id} not found")
            won = conn.execute(
                "UPDATE publications SET status='published', version=version+1,"
                " updated_at=? WHERE id=? AND version=? AND status='uncertain'",
                (stamp, pub_id, expected_version),
            )
            if won.rowcount != 1:
                raise TransitionError(
                    "uncertain resolution lost: publication changed concurrently"
                )
            conn.execute(
                "INSERT INTO published_history(publication_id, post_id,"
                " channel_message_ids, permalink, published_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    pub_id,
                    pub_row["post_id"],
                    json.dumps(sorted(int(m) for m in channel_message_ids)),
                    permalink,
                    stamp,
                ),
            )
        self._journal.record(
            "published",
            post_id=pub_row["post_id"],
            publication_id=pub_id,
            now_epoch=now,
            message_count=len(channel_message_ids),
        )
        return row_to_publication(self._conn.execute(
            "SELECT * FROM publications WHERE id=?", (pub_id,)
        ).fetchone())

    def resolve_uncertain_to_draft(
        self,
        pub_id: int,
        expected_version: int,
        *,
        acknowledged: bool,
        now_epoch: int | None = None,
    ) -> None:
        """Owner verified the post is NOT in the channel and explicitly returns
        it to drafts. Requires an explicit acknowledgement and the current row
        version (compare-and-set): this is a deliberate owner resolution, not
        a silent replan that could duplicate an unknown channel state. The
        minute is released; the post stays a draft until replanned."""
        now = _epoch(now_epoch)
        if acknowledged is not True:
            raise TransitionError(
                "cancel-to-draft of an uncertain publication requires the"
                " owner's explicit acknowledgement that the channel was checked"
            )
        with immediate_tx(self._conn) as conn:
            cur = conn.execute(
                "DELETE FROM publications WHERE id=? AND version=?"
                " AND status='uncertain'",
                (pub_id, expected_version),
            )
            if cur.rowcount != 1:
                raise TransitionError(
                    "uncertain cancel lost: publication changed concurrently"
                    " or is not uncertain"
                )
        self._journal.record(
            "uncertain_cancelled_to_draft", publication_id=pub_id, now_epoch=now
        )

    # ---- recovery -------------------------------------------------------------
    def recovery_sweep(
        self,
        *,
        now_epoch: int | None = None,
        outage_start_epoch: int | None = None,
    ) -> RecoveryResult:
        """Run at startup/reconnect. Idempotent, single transaction:
        1) sending publications with unfinished attempts -> uncertain
           (attempt abandoned); never auto-retried; no channel send here —
           anything whose slot has passed is only marked, never replayed;
        2) scheduled/suspended whose full due window has passed
           (now >= scheduled_epoch + window, i.e. at the next minute onward)
           -> missed (parked, manual replan). A publication still inside its
           window keeps its status so the scheduler may claim it healthily.

        When ``outage_start_epoch`` is given (the end of the last healthy
        heartbeat before this startup/reconnect) the parking rule (2) also
        covers publications whose due time occurred INSIDE the outage gap
        (outage_start_epoch < scheduled_epoch <= now): they are parked even
        when their window has not fully elapsed yet. A minute that began
        while the bot was down is a missed minute — letting it live would
        be exactly the automatic catch-up the plan forbids.
        """
        now = _epoch(now_epoch)
        stamp = iso(now)
        uncertain: list[int] = []
        missed: list[int] = []
        if outage_start_epoch is None:
            overdue_sql = (
                "SELECT id, post_id FROM publications WHERE status IN"
                " ('scheduled','suspended') AND scheduled_epoch + ? <= ?"
                " ORDER BY id"
            )
            overdue_args: tuple = (DISPATCH_WINDOW_SECONDS, now)
        else:
            overdue_sql = (
                "SELECT id, post_id FROM publications WHERE status IN"
                " ('scheduled','suspended') AND scheduled_epoch <= ?"
                " AND (scheduled_epoch + ? <= ? OR scheduled_epoch > ?)"
                " ORDER BY id"
            )
            overdue_args = (now, DISPATCH_WINDOW_SECONDS, now,
                            outage_start_epoch)
        with immediate_tx(self._conn) as conn:
            sending = conn.execute(
                "SELECT id, post_id FROM publications WHERE status='sending'"
                " ORDER BY id"
            ).fetchall()
            for row in sending:
                conn.execute(
                    "UPDATE publications SET status='uncertain',"
                    " version=version+1, updated_at=? WHERE id=?",
                    (stamp, row["id"]),
                )
                abandoned = conn.execute(
                    "UPDATE send_attempts SET state='abandoned' WHERE"
                    " publication_id=? AND state IN ('prepared','in_progress')",
                    (row["id"],),
                )
                for _ in range(abandoned.rowcount):
                    self._journal.record(
                        "attempt_abandoned",
                        publication_id=row["id"],
                        post_id=row["post_id"],
                        now_epoch=now,
                    )
                self._journal.record(
                    "marked_uncertain",
                    publication_id=row["id"],
                    post_id=row["post_id"],
                    now_epoch=now,
                )
                uncertain.append(row["id"])
            overdue = conn.execute(overdue_sql, overdue_args).fetchall()
            for row in overdue:
                conn.execute(
                    "UPDATE publications SET status='missed',"
                    " version=version+1, updated_at=? WHERE id=?",
                    (stamp, row["id"]),
                )
                self._journal.record(
                    "marked_missed",
                    publication_id=row["id"],
                    post_id=row["post_id"],
                    now_epoch=now,
                )
                missed.append(row["id"])
            if uncertain or missed:
                self._journal.record(
                    "recovery_sweep",
                    now_epoch=now,
                    uncertain_count=len(uncertain),
                    missed_count=len(missed),
                )
        return RecoveryResult(tuple(uncertain), tuple(missed))

    # ---- reads ---------------------------------------------------------------
    def unfinished_attempts(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM send_attempts WHERE state IN"
            " ('prepared','in_progress') ORDER BY id"
        ).fetchall()

    def due_publications(self, *, now_epoch: int | None = None) -> list[Publication]:
        """Scheduled publications currently inside their healthy due window
        [scheduled_epoch, scheduled_epoch + window), claim order."""
        now = _epoch(now_epoch)
        rows = self._conn.execute(
            "SELECT * FROM publications WHERE status='scheduled'"
            " AND scheduled_epoch <= ? AND scheduled_epoch + ? > ?"
            " ORDER BY scheduled_epoch, id",
            (now, DISPATCH_WINDOW_SECONDS, now),
        ).fetchall()
        return [row_to_publication(row) for row in rows]

    def content_for_publication(self, pub_id: int) -> Content:
        """The post payload the sender must deliver for this publication."""
        row = self._conn.execute(
            "SELECT p.content_json FROM publications pub"
            " JOIN posts p ON p.id = pub.post_id WHERE pub.id=?",
            (pub_id,),
        ).fetchone()
        if row is None:
            raise PublicationNotFound(f"publication {pub_id} not found")
        return Content.from_json(row["content_json"])

    def overdue_publications(self, *, now_epoch: int | None = None) -> list[Publication]:
        """Scheduled/suspended whose full due window has passed — the sweep's
        missed candidates (nothing inside a live window counts as overdue)."""
        now = _epoch(now_epoch)
        rows = self._conn.execute(
            "SELECT * FROM publications WHERE status IN"
            " ('scheduled','suspended') AND scheduled_epoch + ? <= ?"
            " ORDER BY id",
            (DISPATCH_WINDOW_SECONDS, now),
        ).fetchall()
        return [row_to_publication(row) for row in rows]

    def history_links(self, post_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM published_history WHERE post_id=? ORDER BY id", (post_id,)
        ).fetchall()