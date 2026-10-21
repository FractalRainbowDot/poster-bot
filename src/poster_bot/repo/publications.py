"""Publication lifecycle operations (everything except dispatch/recovery).

Every state change is a compare-and-set: UPDATE ... WHERE id AND version AND
status IN (expected). Exactly one caller wins; losers get TransitionError.
Minute uniqueness is enforced by the DB partial unique index, so two racing
schedules of the same minute cannot both persist.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from ..db import iso, utc_now
from ..domain.content import Content
from ..domain.errors import (
    ConflictError,
    MinuteTakenError,
    PastTimeError,
    PublicationNotFound,
    TransitionError,
)
from ..domain.states import CANCELABLE, Status, require_transition
from ..domain.time import Instant
from ..journal import Journal


@contextmanager
def immediate_tx(conn: sqlite3.Connection):
    """Writer-serialized transaction (BEGIN IMMEDIATE), safe for CAS flows."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _epoch(now_epoch: int | None) -> int:
    return int(utc_now().timestamp()) if now_epoch is None else now_epoch


@dataclass(frozen=True)
class Publication:
    id: int
    post_id: int
    status: Status
    minute_epoch: int
    scheduled_epoch: int
    mode: str
    version: int
    confirmed_at: str | None

    @property
    def scheduled_at(self) -> datetime:
        return datetime.fromtimestamp(self.scheduled_epoch, timezone.utc)


def row_to_publication(row: sqlite3.Row) -> Publication:
    return Publication(
        id=row["id"],
        post_id=row["post_id"],
        status=Status(row["status"]),
        minute_epoch=row["minute_epoch"],
        scheduled_epoch=row["scheduled_epoch"],
        mode=row["mode"],
        version=row["version"],
        confirmed_at=row["confirmed_at"],
    )


class PublicationsService:
    def __init__(self, conn: sqlite3.Connection, journal: Journal) -> None:
        self._conn = conn
        self._journal = journal

    # ---- reads ---------------------------------------------------------
    def get(self, pub_id: int) -> Publication:
        row = self._conn.execute(
            "SELECT * FROM publications WHERE id=?", (pub_id,)
        ).fetchone()
        if row is None:
            raise PublicationNotFound(f"publication {pub_id} not found")
        return row_to_publication(row)

    def get_by_post(self, post_id: int) -> Publication | None:
        row = self._conn.execute(
            "SELECT * FROM publications WHERE post_id=?", (post_id,)
        ).fetchone()
        return row_to_publication(row) if row else None

    def reserved_minutes(self) -> frozenset[int]:
        rows = self._conn.execute(
            "SELECT minute_epoch FROM publications WHERE status IN"
            " ('scheduled','suspended','sending','uncertain','missed')"
        ).fetchall()
        return frozenset(row["minute_epoch"] for row in rows)

    def scheduled_between(self, start_epoch: int,
                          end_epoch: int) -> list[Publication]:
        """All publications scheduled in [start, end) — day plan view,
        every status, in slot order."""
        rows = self._conn.execute(
            "SELECT * FROM publications WHERE scheduled_epoch >= ?"
            " AND scheduled_epoch < ? ORDER BY scheduled_epoch, id",
            (start_epoch, end_epoch),
        ).fetchall()
        return [row_to_publication(row) for row in rows]

    # ---- schedule / confirm ---------------------------------------------
    def schedule(
        self, post_id: int, instant: Instant, *, now_epoch: int | None = None
    ) -> Publication:
        """Draft -> scheduled on a strictly-future instant, unique minute."""
        now = _epoch(now_epoch)
        if instant.epoch <= now:
            raise PastTimeError(
                f"publication time {instant.iso} is not strictly in the future"
            )
        with immediate_tx(self._conn) as conn:
            post = conn.execute(
                "SELECT content_json FROM posts WHERE id=?", (post_id,)
            ).fetchone()
            if post is None:
                raise PublicationNotFound(f"post {post_id} not found")
            content = Content.from_json(post["content_json"])
            stamp = iso(now)
            try:
                cur = conn.execute(
                    "INSERT INTO publications(post_id, status, minute_epoch,"
                    " scheduled_epoch, scheduled_at, mode, version, created_at,"
                    " updated_at, confirmed_at)"
                    " VALUES (?, 'scheduled', ?, ?, ?, ?, 1, ?, ?, ?)",
                    (
                        post_id,
                        instant.minute,
                        instant.epoch,
                        instant.iso,
                        content.mode,
                        stamp,
                        stamp,
                        stamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise self._integrity_error(exc, post_id, instant) from None
            pub_id = int(cur.lastrowid)
        self._journal.record(
            "scheduled",
            post_id=post_id,
            publication_id=pub_id,
            now_epoch=now,
            minute_epoch=instant.minute,
        )
        return self.get(pub_id)

    def resume(
        self, pub_id: int, expected_version: int, *, now_epoch: int | None = None
    ) -> Publication:
        """Suspended -> scheduled on re-confirmation; the stored instant must
        still be strictly future, otherwise the owner must replan."""
        pub = self.get(pub_id)
        if pub.status is not Status.SUSPENDED:
            raise TransitionError(
                "only a suspended publication can be re-confirmed on its stored"
                " slot; missed or uncertain ones must be replanned to a new minute"
            )
        now = _epoch(now_epoch)
        if pub.scheduled_epoch <= now:
            raise PastTimeError(
                "the stored slot is no longer in the future; replan manually"
            )
        self._cas(
            pub_id,
            expected_version,
            {Status.SUSPENDED},
            "status='scheduled', confirmed_at=?",
            (iso(now),),
            now,
        )
        self._journal.record(
            "resumed", publication_id=pub_id, post_id=pub.post_id, now_epoch=now
        )
        return self.get(pub_id)

    def replan(
        self,
        pub_id: int,
        expected_version: int,
        new_instant: Instant,
        *,
        now_epoch: int | None = None,
    ) -> Publication:
        """Missed/uncertain -> scheduled on a new strictly-future minute
        (manual only; no automatic moves anywhere)."""
        pub = self.get(pub_id)
        require_transition(pub.status, Status.SCHEDULED)
        now = _epoch(now_epoch)
        if new_instant.epoch <= now:
            raise PastTimeError(
                "replanning needs a strictly-future minute; automatic catch-up"
                " is not allowed"
            )
        stamp = iso(now)
        with immediate_tx(self._conn) as conn:
            try:
                cur = conn.execute(
                    "UPDATE publications SET status='scheduled',"
                    " minute_epoch=?, scheduled_epoch=?, scheduled_at=?,"
                    " confirmed_at=?, version=version+1, updated_at=?"
                    " WHERE id=? AND version=? AND status IN ('missed','uncertain')",
                    (
                        new_instant.minute,
                        new_instant.epoch,
                        new_instant.iso,
                        stamp,
                        stamp,
                        pub_id,
                        expected_version,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise self._integrity_error(exc, pub.post_id, new_instant) from None
            if cur.rowcount != 1:
                raise TransitionError(
                    "replan lost: publication was changed concurrently"
                )
        self._journal.record(
            "scheduled",
            post_id=pub.post_id,
            publication_id=pub_id,
            now_epoch=now,
            minute_epoch=new_instant.minute,
        )
        return self.get(pub_id)

    # ---- edit / pause -----------------------------------------------------
    def edit_time(
        self,
        pub_id: int,
        expected_version: int,
        new_instant: Instant,
        *,
        now_epoch: int | None = None,
    ) -> Publication:
        """Owner-changed time for a scheduled/suspended publication: a new
        strictly-future unique minute, re-confirmed by the explicit owner
        confirm press. Still no automatic moves anywhere."""
        pub = self.get(pub_id)
        if pub.status not in (Status.SCHEDULED, Status.SUSPENDED):
            raise TransitionError(
                "only scheduled or suspended publications can be given a new"
                " time; missed or uncertain ones go through replanning"
            )
        now = _epoch(now_epoch)
        if new_instant.epoch <= now:
            raise PastTimeError(
                "the new publication time must be strictly in the future"
            )
        stamp = iso(now)
        with immediate_tx(self._conn) as conn:
            try:
                cur = conn.execute(
                    "UPDATE publications SET status='scheduled',"
                    " minute_epoch=?, scheduled_epoch=?, scheduled_at=?,"
                    " confirmed_at=?, version=version+1, updated_at=?"
                    " WHERE id=? AND version=?"
                    " AND status IN ('scheduled','suspended')",
                    (
                        new_instant.minute,
                        new_instant.epoch,
                        new_instant.iso,
                        stamp,
                        stamp,
                        pub_id,
                        expected_version,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise self._integrity_error(exc, pub.post_id, new_instant) \
                    from None
            if cur.rowcount != 1:
                raise TransitionError(
                    "time change lost: publication was changed concurrently"
                )
        self._journal.record(
            "scheduled",
            post_id=pub.post_id,
            publication_id=pub_id,
            now_epoch=now,
            minute_epoch=new_instant.minute,
        )
        return self.get(pub_id)

    def edit_content(
        self,
        pub_id: int,
        expected_version: int,
        new_content: Content,
        *,
        now_epoch: int | None = None,
    ) -> Publication:
        """Editing is allowed only while scheduled; it stores new content and
        parks the publication as suspended until re-confirmation (PLAN §2)."""
        pub = self.get(pub_id)
        require_transition(pub.status, Status.SUSPENDED)
        now = _epoch(now_epoch)
        stamp = iso(now)
        payload = new_content.to_json()  # validates before any write
        with immediate_tx(self._conn) as conn:
            cur = conn.execute(
                "UPDATE posts SET content_json=?, updated_at=? WHERE id=?",
                (payload, stamp, pub.post_id),
            )
            assert cur.rowcount == 1
            won = conn.execute(
                "UPDATE publications SET status='suspended',"
                " version=version+1, updated_at=?"
                " WHERE id=? AND version=? AND status='scheduled'",
                (stamp, pub_id, expected_version),
            )
            if won.rowcount != 1:
                raise TransitionError(
                    "edit lost: only a scheduled publication can be edited and"
                    " it was changed concurrently"
                )
        self._journal.record(
            "edited_paused",
            post_id=pub.post_id,
            publication_id=pub_id,
            now_epoch=now,
        )
        return self.get(pub_id)

    def pause(
        self, pub_id: int, expected_version: int, *, now_epoch: int | None = None
    ) -> Publication:
        pub = self.get(pub_id)
        require_transition(pub.status, Status.SUSPENDED)
        now = _epoch(now_epoch)
        self._cas(
            pub_id,
            expected_version,
            {Status.SCHEDULED},
            "status='suspended'",
            (),
            now,
        )
        self._journal.record(
            "paused", publication_id=pub_id, post_id=pub.post_id, now_epoch=now
        )
        return self.get(pub_id)

    # ---- cancel -----------------------------------------------------------
    def cancel_to_draft(
        self, pub_id: int, expected_version: int, *, now_epoch: int | None = None
    ) -> None:
        """Scheduled/suspended/missed -> back to drafts (minute released)."""
        pub = self._require_cancelable(pub_id, expected_version)
        now = _epoch(now_epoch)
        with immediate_tx(self._conn) as conn:
            cur = conn.execute(
                "DELETE FROM publications WHERE id=? AND version=?"
                " AND status IN ('scheduled','suspended','missed')",
                (pub_id, expected_version),
            )
            if cur.rowcount != 1:
                raise TransitionError("cancel lost: publication changed concurrently")
        self._journal.record(
            "cancelled_to_draft",
            post_id=pub.post_id,
            publication_id=pub_id,
            now_epoch=now,
        )

    def delete_post(
        self, pub_id: int, expected_version: int, *, now_epoch: int | None = None
    ) -> None:
        """Hard delete: the post and everything hanging off it (publication,
        attempts, history links) go away; the minute is released."""
        pub = self._require_cancelable(pub_id, expected_version)
        now = _epoch(now_epoch)
        with immediate_tx(self._conn) as conn:
            cas = conn.execute(
                "SELECT id FROM publications WHERE id=? AND version=?"
                " AND status IN ('scheduled','suspended','missed')",
                (pub_id, expected_version),
            ).fetchone()
            if cas is None:
                raise TransitionError("delete lost: publication changed concurrently")
            conn.execute("DELETE FROM posts WHERE id=?", (pub.post_id,))
        self._journal.record(
            "post_deleted",
            post_id=pub.post_id,
            publication_id=pub_id,
            now_epoch=now,
        )

    # ---- internals ---------------------------------------------------------
    def delete_by_post(self, post_id: int, *, now_epoch: int | None = None) -> None:
        """Owner deletes a post entirely (history link dies with it). Allowed
        unless a publication is in flight or unconfirmed — those are immutable
        until the owner resolves them."""
        now = _epoch(now_epoch)
        with immediate_tx(self._conn) as conn:
            row = conn.execute(
                "SELECT status FROM publications WHERE post_id=?", (post_id,)
            ).fetchone()
            if row is not None and row["status"] in ("sending", "uncertain"):
                raise TransitionError(
                    "cannot delete: the publication is in flight or unconfirmed"
                )
            cur = conn.execute("DELETE FROM posts WHERE id=?", (post_id,))
            if cur.rowcount != 1:
                raise PublicationNotFound(f"post {post_id} not found")
        self._journal.record("post_deleted", post_id=post_id, now_epoch=now)

    def _require_cancelable(self, pub_id: int, expected_version: int) -> Publication:
        pub = self.get(pub_id)
        if pub.status not in CANCELABLE:
            raise TransitionError(
                f"cannot cancel a {pub.status.value} publication; an in-flight"
                " or unconfirmed send is immutable until the owner resolves it"
            )
        return pub

    def _cas(
        self,
        pub_id: int,
        expected_version: int,
        expected_status: set[Status],
        sets: str,
        set_values: tuple[object, ...],
        now_epoch: int,
    ) -> None:
        placeholders = ",".join("?" for _ in expected_status)
        stamp = iso(now_epoch)
        with immediate_tx(self._conn) as conn:
            cur = conn.execute(
                f"UPDATE publications SET {sets}, version=version+1,"
                f" updated_at=? WHERE id=? AND version=?"
                f" AND status IN ({placeholders})",
                (
                    *set_values,
                    stamp,
                    pub_id,
                    expected_version,
                    *(s.value for s in expected_status),
                ),
            )
            if cur.rowcount != 1:
                raise TransitionError(
                    f"publication {pub_id} was changed concurrently"
                    " (compare-and-set lost)"
                )

    @staticmethod
    def _integrity_error(
        exc: sqlite3.IntegrityError, post_id: int, instant: Instant
    ) -> Exception:
        message = str(exc)
        if "publications.post_id" in message:
            return ConflictError(f"post {post_id} already has a publication")
        if "minute_epoch" in message:
            return MinuteTakenError(
                f"minute {instant.iso} is already reserved by another publication"
            )
        return TransitionError(f"schedule rejected: {message}")