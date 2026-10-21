"""Server-issued action tokens for owner keyboards.

Every callback button carries only a short token (well under Telegram's
64-byte callback-data limit); everything the button means — action kind,
post/publication ids, expected row version, chosen minute — is stored
server-side and re-validated when the token is executed. A forged token,
a replayed old confirmation, or a press after the state moved therefore
fails closed: ``consume`` only ever succeeds once, and the flow re-checks
status/version/time/minute against storage before any mutation.
"""
from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass

from ..db import iso, utc_now
from ..domain.errors import DomainError

PENDING = "pending"
CONSUMED = "consumed"
INVALIDATED = "invalidated"


class ActionStale(DomainError):
    """Token unknown, already consumed, or invalidated by a newer step."""


@dataclass(frozen=True)
class Action:
    token: str
    action: str
    post_id: int | None
    publication_id: int | None
    expected_version: int | None
    minute_epoch: int | None
    arg: str | None
    status: str


def _row_to_action(row: sqlite3.Row) -> Action:
    return Action(
        token=row["token"],
        action=row["action"],
        post_id=row["post_id"],
        publication_id=row["publication_id"],
        expected_version=row["expected_version"],
        minute_epoch=row["minute_epoch"],
        arg=row["arg"],
        status=row["status"],
    )


def _new_token() -> str:
    return "a" + secrets.token_hex(6)


class ActionStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create(
        self,
        action: str,
        *,
        post_id: int | None = None,
        publication_id: int | None = None,
        expected_version: int | None = None,
        minute_epoch: int | None = None,
        arg: str | None = None,
        now_epoch: int | None = None,
    ) -> str:
        stamp = iso(int(utc_now().timestamp()) if now_epoch is None else now_epoch)
        for _ in range(5):  # token collisions are astronomically rare; retry anyway
            token = _new_token()
            with self._conn:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO owner_actions(token, action, post_id,"
                    " publication_id, expected_version, minute_epoch, arg, status,"
                    " created_at) VALUES (?,?,?,?,?,?,?, 'pending', ?)",
                    (
                        token,
                        action,
                        post_id,
                        publication_id,
                        expected_version,
                        minute_epoch,
                        arg,
                        stamp,
                    ),
                )
            if cur.rowcount == 1:
                return token
        raise DomainError("could not allocate an action token")

    def peek(self, token: str) -> Action | None:
        row = self._conn.execute(
            "SELECT * FROM owner_actions WHERE token=?", (token,)
        ).fetchone()
        return _row_to_action(row) if row is not None else None

    def consume(self, token: str, *, now_epoch: int | None = None) -> Action:
        """Atomic test-and-set: exactly one press of a pending token wins."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE owner_actions SET status=? WHERE token=? AND status=?",
                (CONSUMED, token, PENDING),
            )
        action = self.peek(token)
        if action is None or cur.rowcount != 1:
            raise ActionStale("this button is no longer valid")
        return action

    def invalidate(
        self,
        *,
        publication_id: int | None = None,
        post_id: int | None = None,
        except_token: str | None = None,
    ) -> int:
        """Best-effort hygiene: supersede still-pending buttons that target
        this post/publication (execution-time revalidation stays the real
        guard; this only tidies up old keyboards)."""
        clauses, values = [], []
        if publication_id is not None:
            clauses.append("publication_id=?")
            values.append(publication_id)
        if post_id is not None:
            clauses.append("post_id=?")
            values.append(post_id)
        if not clauses:
            return 0
        sql = (
            "UPDATE owner_actions SET status='invalidated'"
            f" WHERE status='pending' AND ({' OR '.join(clauses)})"
        )
        if except_token is not None:
            sql += " AND token<>?"
            values.append(except_token)
        with self._conn:
            cur = self._conn.execute(sql, tuple(values))
        return cur.rowcount