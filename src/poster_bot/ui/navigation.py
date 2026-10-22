"""Durable identity of the service card, separate from conversation state.

Only IDs returned by our own sendMessage are recorded. Never infer ownership
from callback message text, sender, or a client-supplied message ID.
"""
from __future__ import annotations

import json
import sqlite3

from ..db import iso
from ..repo.publications import immediate_tx


class NavigationStore:
    def __init__(self, conn: sqlite3.Connection, owner_id: int) -> None:
        self._conn = conn
        self._key = f"navigation:{owner_id}"

    def get(self, chat_id: int) -> int | None:
        row = self._conn.execute(
            "SELECT value FROM ui_state WHERE key=?", (self._key,)
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row["value"])
        return value["message_id"] if value["chat_id"] == chat_id else None

    def set(self, chat_id: int, message_id: int, *, now: int) -> None:
        self._write(self._key, chat_id, message_id, now=now)

    def keyboard_installed(self, chat_id: int) -> bool:
        return self.keyboard_message(chat_id) is not None

    def keyboard_message(self, chat_id: int) -> int | None:
        row = self._conn.execute(
            "SELECT value FROM ui_state WHERE key=?", (self._key + ":keyboard",)
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row["value"])
        return value["message_id"] if value["chat_id"] == chat_id else None

    def record_keyboard(self, chat_id: int, message_id: int, *, now: int) -> None:
        self._write(self._key + ":keyboard", chat_id, message_id, now=now)

    def _write(self, key: str, chat_id: int, message_id: int, *, now: int) -> None:
        with immediate_tx(self._conn) as conn:
            conn.execute(
                "INSERT INTO ui_state(key,value,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, json.dumps({"chat_id": chat_id,
                                       "message_id": message_id}), iso(now)),
            )
