from __future__ import annotations

from poster_bot.db import connect, current_version, migrate
from poster_bot.migrations import CURRENT_VERSION, MIGRATIONS

EXPECTED_TABLES = {
    "posts",
    "publications",
    "send_attempts",
    "published_history",
    "settings",
    "manual_times",
    "journal_events",
    "schema_migrations",
    "runtime_state",
    "owner_notices",
    "processed_updates",
}


def test_fresh_db_creates_file_and_tables(tmp_path):
    db_path = str(tmp_path / "fresh" / "bot.sqlite3")
    conn = connect(db_path)
    assert migrate(conn) == [version for version, _, _ in MIGRATIONS]
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert EXPECTED_TABLES <= names
    index = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_reserved_minute'"
    ).fetchone()
    assert index is not None
    conn.close()


def test_migrate_idempotent(tmp_path):
    conn = connect(str(tmp_path / "bot.sqlite3"))
    assert migrate(conn) == [version for version, _, _ in MIGRATIONS]
    assert migrate(conn) == []
    assert current_version(conn) == CURRENT_VERSION
    rows = conn.execute("SELECT COUNT(*) c FROM schema_migrations").fetchone()
    assert rows["c"] == CURRENT_VERSION
    conn.close()


def test_upgrade_from_v1_only_adds_v2(tmp_path):
    """A database built at v1 upgrades in place: runtime/outbox/dedup tables
    appear, existing rows untouched."""
    from poster_bot.migrations import _V1

    db_path = str(tmp_path / "v1.sqlite3")
    conn = connect(db_path)
    conn.execute("BEGIN")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               name TEXT NOT NULL,
               applied_at TEXT NOT NULL
           )"""
    )
    for statement in _V1:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO schema_migrations(version, name, applied_at) VALUES"
        " (1, 'baseline', '2026-10-21T00:00:00+00:00')"
    )
    conn.execute("COMMIT")
    assert current_version(conn) == 1
    assert migrate(conn) == [
        version for version, _, _ in MIGRATIONS if version > 1
    ]
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "runtime_state", "owner_notices", "processed_updates",
        "ui_state", "owner_actions", "inbox_updates",
    } <= names
    assert current_version(conn) == CURRENT_VERSION
    conn.close()


def test_pragmas_enabled(tmp_path):
    conn = connect(str(tmp_path / "bot.sqlite3"))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()