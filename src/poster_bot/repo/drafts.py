"""Draft store. A draft IS a post without any publication row: drafts live
until deleted, survive restarts and finish-of-session (content lives in the
posts table; scheduling adds a publication row, cancel-to-draft removes it).
"""
from __future__ import annotations

import sqlite3

from ..db import iso, utc_now
from ..domain.content import Content
from ..domain.errors import DomainError


class DraftNotFound(DomainError):
    pass


def _now_iso(now_epoch: int | None) -> str:
    return iso(int(utc_now().timestamp()) if now_epoch is None else now_epoch)


class DraftStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create(
        self, content: Content, *, now_epoch: int | None = None
    ) -> int:
        now = _now_iso(now_epoch)
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO posts(content_json, created_at, updated_at)"
                " VALUES (?, ?, ?)",
                (content.to_json(), now, now),
            )
        return int(cur.lastrowid)

    def update(
        self,
        post_id: int,
        content: Content,
        *,
        now_epoch: int | None = None,
    ) -> None:
        """Edit a draft only; scheduled content changes go through the
        publications service (editing pauses the publication)."""
        now = _now_iso(now_epoch)
        with self._conn:
            cur = self._conn.execute(
                "UPDATE posts SET content_json=?, updated_at=?"
                " WHERE id=? AND NOT EXISTS"
                " (SELECT 1 FROM publications p WHERE p.post_id = posts.id)",
                (content.to_json(), now, post_id),
            )
        if cur.rowcount == 0:
            raise DraftNotFound(f"draft {post_id} not found or is not a draft")

    def get_content(self, post_id: int) -> Content:
        row = self._conn.execute(
            "SELECT content_json FROM posts WHERE id=?", (post_id,)
        ).fetchone()
        if row is None:
            raise DraftNotFound(f"post {post_id} not found")
        return Content.from_json(row["content_json"])

    def is_draft(self, post_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM posts p WHERE p.id=? AND NOT EXISTS"
            " (SELECT 1 FROM publications pub WHERE pub.post_id = p.id)",
            (post_id,),
        ).fetchone()
        return row is not None

    def list_drafts(self, *, limit: int = 100) -> list[int]:
        rows = self._conn.execute(
            "SELECT p.id FROM posts p WHERE NOT EXISTS"
            " (SELECT 1 FROM publications pub WHERE pub.post_id = p.id)"
            " ORDER BY p.updated_at DESC, p.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [row["id"] for row in rows]

    def delete(self, post_id: int) -> None:
        """Delete a draft (a post that has no publication). Posts with an
        active or historical publication must be cancelled through the
        publications service first."""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM posts WHERE id=? AND NOT EXISTS"
                " (SELECT 1 FROM publications pub WHERE pub.post_id = posts.id)",
                (post_id,),
            )
        if cur.rowcount == 0:
            raise DraftNotFound(f"draft {post_id} not found or is not a draft")