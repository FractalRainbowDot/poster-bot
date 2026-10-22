"""Durable conversation state (single-owner bot: one conversation row).

The state name plus a JSON payload persist on every step, so a restart never
loses an in-progress collection (including a partially received album) and a
stale step loses against a newer one through the compare-and-set version.
Payload is code-written JSON only — never owner secrets, and never channel
content beyond what the next step legitimately needs (file ids, ids, epochs).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from ..db import iso, utc_now
from ..domain.errors import DomainError
from ..repo.publications import immediate_tx

KEY_CONVERSATION = "conversation"


class State(StrEnum):
    MENU = "menu"                      # main menu shown
    CREATING = "creating"              # collecting content for a new post
    EDITING = "editing"                # replacing content of an existing post
    AWAIT_REPLACE = "await_replace"    # new content arrived; replace? explicit
    MANUAL_TIME = "manual_time"        # awaiting a wall-clock time text
    CONFIRM_SCHEDULE = "confirm"       # summary shown; awaiting confirm press
    DRAFT = "draft"                    # viewing one draft
    PUBLICATION = "publication"        # viewing one publication
    SETTINGS = "settings"              # settings menu
    SETTINGS_INPUT = "settings_input"  # awaiting a settings value text
    RESOLUTION = "resolution"          # awaiting channel link/message id input
    ACK_RISK = "ack_risk"              # awaiting explicit risk acknowledgement


class StaleStateError(DomainError):
    """The conversation moved on: this step lost the compare-and-set."""


class EffectAlreadyApplied(DomainError):
    """The durable effect behind this message was already committed once.

    Raised when a redelivered update (a crash landed between the handler's
    committed effect and inbox.complete) tries to apply the same per-message
    effect a second time. The compare-and-set transaction rolls back without
    touching the conversation, so the flow can answer idempotently."""


@dataclass(frozen=True)
class UiState:
    state: State
    payload: dict
    version: int

    def arg(self, name: str, default=None):
        return self.payload.get(name, default)


def _decode(value: str) -> UiState:
    data = json.loads(value)
    return UiState(State(data["state"]), data["payload"], int(data["version"]))


class UiStateRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self) -> UiState:
        row = self._conn.execute(
            "SELECT value FROM ui_state WHERE key=?", (KEY_CONVERSATION,)
        ).fetchone()
        if row is None:
            return UiState(State.MENU, {}, 0)
        return _decode(row["value"])

    def set(
        self,
        state: State,
        payload: dict,
        *,
        expected_version: int | None = None,
        now_epoch: int | None = None,
        effect_key: str | None = None,
    ) -> UiState:
        """Write the conversation inside one immediate transaction: the row
        is read and rewritten atomically, and an optional expected_version
        makes stale writers lose with StaleStateError.

        ``effect_key`` makes the write the single durable record of one
        incoming message's effect: the key row and the conversation write
        commit or roll back together, so a redelivered update whose effect
        already landed loses with EffectAlreadyApplied instead of applying
        it twice."""
        now = iso(int(utc_now().timestamp()) if now_epoch is None else now_epoch)
        with immediate_tx(self._conn) as conn:
            if effect_key is not None:
                marker = conn.execute(
                    "INSERT INTO ui_effects(effect_key, applied_at)"
                    " VALUES (?, ?) ON CONFLICT (effect_key) DO NOTHING",
                    (effect_key, now),
                )
                if marker.rowcount == 0:
                    raise EffectAlreadyApplied(
                        f"effect {effect_key!r} was already applied once"
                    )
            row = conn.execute(
                "SELECT value FROM ui_state WHERE key=?", (KEY_CONVERSATION,)
            ).fetchone()
            current = _decode(row["value"]) if row is not None else UiState(
                State.MENU, {}, 0
            )
            if expected_version is not None and current.version != expected_version:
                raise StaleStateError(
                    "the conversation moved on since this step (a newer step wins)"
                )
            value = json.dumps(
                {
                    "state": str(state),
                    "payload": payload,
                    "version": current.version + 1,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            if row is None:
                conn.execute(
                    "INSERT INTO ui_state(key, value, updated_at) VALUES (?,?,?)",
                    (KEY_CONVERSATION, value, now),
                )
            else:
                conn.execute(
                    "UPDATE ui_state SET value=?, updated_at=? WHERE key=?",
                    (value, now, KEY_CONVERSATION),
                )
        return self.get()

    def has_effect(self, effect_key: str) -> bool:
        """True when this per-message effect is already durably recorded.

        Read-only companion to ``set(effect_key=...)``: lets an intake path
        recognize a redelivered message even in conversation states where a
        fresh message would reset the conversation — a replay must never
        destroy the newer step."""
        row = self._conn.execute(
            "SELECT 1 FROM ui_effects WHERE effect_key=?", (effect_key,)
        ).fetchone()
        return row is not None

    def record_effect(self, effect_key: str, *, now_epoch: int) -> None:
        """Receipt for a completed non-content input; preserve issued versions.

        Content intake uses set(effect_key=...) to commit content and receipt
        together. This method must only follow a successful durable operation.
        """
        with immediate_tx(self._conn) as conn:
            conn.execute(
                "INSERT INTO ui_effects(effect_key, applied_at) VALUES (?, ?) "
                "ON CONFLICT(effect_key) DO NOTHING", (effect_key, iso(now_epoch)),
            )

    def reset_to_menu(self, *, now_epoch: int | None = None) -> UiState:
        return self.set(State.MENU, {}, now_epoch=now_epoch)
