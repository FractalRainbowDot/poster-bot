"""SQLite connections and the migration runner.

All stored instants are UTC epoch seconds (integers). ISO strings are stored
next to them for readability only; comparisons and ordering use epochs.
This keeps scheduled publications stable across timezone/settings changes.

Privacy: the database holds the owner's private post text. SQLite would
create a fresh database (and its -wal/-shm sidecars) with 0666 & ~umask,
which is world-readable under a permissive umask. ``connect`` therefore
pre-creates the database file and sidecars with 0600 and gives newly created
data directories 0700 (umask-proof chmod). Anything that already exists is
never chmod'ed: user-set paths and modes are left exactly as they are.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from .migrations import CURRENT_VERSION, MIGRATIONS


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _ensure_private_dir(path: str) -> None:
    """Create ``path`` (and missing ancestors) with mode 0700.

    Only directories this call actually creates are chmod'ed to 0700 so the
    result does not depend on the process umask. Existing directories -- the
    user's paths included -- are never touched."""
    missing: list[str] = []
    current = path
    while not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:  # reached the root without finding a directory
            break
        current = parent
    if not missing:
        return
    os.makedirs(path, mode=0o700, exist_ok=True)
    for created in missing:
        os.chmod(created, 0o700)


def _private_file(path: str) -> None:
    """Create an empty file with mode 0600 if absent; never re-modes one.

    Pre-creating the database and its WAL sidecars is what keeps them
    owner-only under a permissive umask: SQLite opens existing files as-is
    instead of creating them 0666 & ~umask, and a pre-existing user file
    keeps whatever mode its owner chose."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)


def connect(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(db_path))
    _ensure_private_dir(parent)
    if db_path != ":memory:":
        # the sidecar names SQLite derives from this path; pre-creating them
        # before the first WAL write is what pins them to 0600
        for suffix in ("", "-wal", "-shm"):
            _private_file(db_path + suffix)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return 0
    return conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()[0]


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations, each in its own transaction. Idempotent."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               name TEXT NOT NULL,
               applied_at TEXT NOT NULL
           )"""
    )
    applied: list[int] = []
    done = {
        row["version"]
        for row in conn.execute("SELECT version FROM schema_migrations")
    }
    for version, name, statements in MIGRATIONS:
        if version in done:
            continue
        if version != (max(done) if done else 0) + 1:
            raise RuntimeError(f"migration sequence gap at version {version}")
        with conn:  # one transaction per migration (statement-by-statement,
            # never executescript: it would commit outside the transaction)
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at)"
                " VALUES (?, ?, ?)",
                (version, name, iso(int(utc_now().timestamp()))),
            )
        done.add(version)
        applied.append(version)
    assert current_version(conn) == CURRENT_VERSION
    return applied