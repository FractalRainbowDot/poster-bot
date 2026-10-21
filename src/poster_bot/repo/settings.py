"""Settings store (DB-backed; env provides initial values only).

Changing timezone or slot hours here never touches publications: scheduled
instants are UTC epochs and stay put (PLAN §2, Phase 5 acceptance).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..db import iso, utc_now
from ..config import Config
from ..domain.errors import DomainError

KEY_START = "slot_start_hour"
KEY_END = "slot_end_hour"
KEY_TZ = "timezone"
EDITABLE_KEYS = (KEY_START, KEY_END, KEY_TZ)


class SettingsError(DomainError):
    pass


@dataclass(frozen=True)
class ScheduleSettings:
    timezone: str
    start_hour: int
    end_hour: int


def _validate(key: str, value: str) -> None:
    if key in (KEY_START, KEY_END):
        try:
            hour = int(value)
        except ValueError:
            raise SettingsError(f"{key} must be an integer hour") from None
        if not 0 <= hour <= 23:
            raise SettingsError(f"{key} must be 0..23")
    elif key == KEY_TZ:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise SettingsError(f"{key} is not a loadable IANA timezone") from None
    else:
        raise SettingsError(f"unknown setting {key!r}")


class SettingsRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def seed_defaults(self, config: Config) -> None:
        """Insert keys absent from the DB using env-loaded config values."""
        values = {
            KEY_START: str(config.slot_start_hour),
            KEY_END: str(config.slot_end_hour),
            KEY_TZ: config.timezone,
        }
        now = iso(int(utc_now().timestamp()))
        with self._conn:
            for key, value in values.items():
                self._conn.execute(
                    "INSERT INTO settings(key, value, updated_at) VALUES (?,?,?)"
                    " ON CONFLICT(key) DO NOTHING",
                    (key, value, now),
                )

    def get(self) -> ScheduleSettings:
        rows = {
            row["key"]: row["value"]
            for row in self._conn.execute("SELECT key, value FROM settings")
        }
        try:
            return ScheduleSettings(
                timezone=rows[KEY_TZ],
                start_hour=int(rows[KEY_START]),
                end_hour=int(rows[KEY_END]),
            )
        except KeyError as exc:
            raise SettingsError(f"missing setting {exc.args[0]!r}") from None

    def change(self, **updates: str | int) -> ScheduleSettings:
        """Update allowed settings; existing publications are left untouched."""
        for key in updates:
            if key not in EDITABLE_KEYS:
                raise SettingsError(f"setting {key!r} is not editable")
        current = self._raw_map()
        merged = {**current, **{k: str(v) for k, v in updates.items()}}
        for key in EDITABLE_KEYS:
            _validate(key, merged[key])
        if int(merged[KEY_START]) > int(merged[KEY_END]):
            raise SettingsError("slot_start_hour must not exceed slot_end_hour")
        now = iso(int(utc_now().timestamp()))
        with self._conn:
            for key, value in updates.items():
                self._conn.execute(
                    "UPDATE settings SET value=?, updated_at=? WHERE key=?",
                    (str(value), now, key),
                )
        return self.get()

    def _raw_map(self) -> dict[str, str]:
        return {
            row["key"]: row["value"]
            for row in self._conn.execute("SELECT key, value FROM settings")
        }