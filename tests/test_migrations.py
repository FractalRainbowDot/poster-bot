from __future__ import annotations

from poster_bot.db import connect, current_version, iso, migrate
from poster_bot.migrations import CURRENT_VERSION, MIGRATIONS
from conftest import NOW

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
    "post_plans",
    "publication_parts",
    "navigation_edits",
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


def _v4_db(tmp_path):
    """A database exactly as the pre-CHUNK2 release shipped it (V1..V4)."""
    conn = connect(str(tmp_path / "v4.sqlite3"))
    conn.execute("BEGIN")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               name TEXT NOT NULL,
               applied_at TEXT NOT NULL
           )"""
    )
    for version, name, statements in MIGRATIONS[:4]:
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations(version, name, applied_at)"
            " VALUES (?, ?, '2026-10-21T00:00:00+00:00')",
            (version, name),
        )
    conn.execute("COMMIT")
    return conn


def _legacy_dump(conn):
    tables = ("posts", "publications", "send_attempts", "published_history")
    return {
        table: [tuple(row) for row in conn.execute(
            f"SELECT * FROM {table} ORDER BY id")]
        for table in tables
    }


def _v4_send_state(conn, pub_id: int, status: str, attempt_state: str,
                   outcome, finished_at=None):
    """Put a scheduled publication into a post-send state using raw v4 SQL:
    the v5 claim queries post_plans, which does not exist yet at v4."""
    conn.execute("UPDATE publications SET status=? WHERE id=?",
                 (status, pub_id))
    conn.execute(
        "INSERT INTO send_attempts (publication_id, state, outcome,"
        " created_at, finished_at) VALUES (?,?,?,?,?)",
        (pub_id, attempt_state, outcome, iso(NOW), finished_at))


class TestUpgradeV4ToV5:
    """Old rows in every publication status survive the CHUNK2 migration
    byte-for-byte, and legacy single-content sending keeps working on the
    upgraded database (no part rows are invented for planless posts)."""

    def test_every_status_survives_byte_for_byte(self, tmp_path):
        from poster_bot.journal import Journal
        from poster_bot.repo.dispatch import DispatchService
        from poster_bot.repo.drafts import DraftStore
        from poster_bot.repo.publications import PublicationsService
        from conftest import NOW, future, text_content

        conn = _v4_db(tmp_path)
        assert current_version(conn) == 4
        journal = Journal(conn)
        drafts = DraftStore(conn)
        pubs = PublicationsService(conn, journal)
        disp = DispatchService(conn, journal)

        def new_post(tag: str) -> int:
            return drafts.create(text_content(f"legacy body {tag}"),
                                 now_epoch=NOW)

        pubs.schedule(new_post("scheduled"), future(1), now_epoch=NOW)
        suspended = pubs.schedule(new_post("suspended"), future(2),
                                  now_epoch=NOW)
        pubs.pause(suspended.id, suspended.version, now_epoch=NOW)
        sending = pubs.schedule(new_post("sending"), future(3), now_epoch=NOW)
        _v4_send_state(conn, sending.id, "sending", "in_progress", None)
        done_post = new_post("published")
        done = pubs.schedule(done_post, future(4), now_epoch=NOW)
        _v4_send_state(conn, done.id, "published", "finished", "published",
                       finished_at=iso(NOW + 241))
        conn.execute(
            "INSERT INTO published_history (publication_id, post_id,"
            " channel_message_ids, permalink, published_at)"
            " VALUES (?,?,?,?,?)",
            (done.id, done_post, '["4001"]', "https://t.me/c/777/4001",
             iso(NOW + 241)))
        stuck = pubs.schedule(new_post("uncertain"), future(5), now_epoch=NOW)
        _v4_send_state(conn, stuck.id, "uncertain", "in_progress", None)
        failed = pubs.schedule(new_post("missed"), future(6), now_epoch=NOW)
        _v4_send_state(conn, failed.id, "missed", "abandoned",
                       "definitive_failure", finished_at=iso(NOW + 361))
        conn.commit()

        before = _legacy_dump(conn)
        assert before["publications"] and len(before["posts"]) == 6
        assert sorted(row[2] for row in before["publications"]) == sorted(
            ["scheduled", "suspended", "sending", "published", "uncertain",
             "missed"])

        assert migrate(conn) == [5]
        assert _legacy_dump(conn) == before
        assert current_version(conn) == CURRENT_VERSION
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert migrate(conn) == []

        # legacy single-content flow keeps working after the upgrade
        post_id = drafts.create(text_content("post-upgrade"), now_epoch=NOW)
        pub = pubs.schedule(post_id, future(7), now_epoch=NOW)
        attempt = disp.claim(pub.id, pub.version, now_epoch=NOW + 420)
        disp.complete(pub.id, attempt, (5001,), "https://t.me/c/777/5001",
                      now_epoch=NOW + 421)
        parts = conn.execute(
            "SELECT COUNT(*) c FROM publication_parts WHERE attempt_id=?",
            (attempt,),
        ).fetchone()
        assert parts["c"] == 0
        conn.close()

    def test_v5_tables_and_constraints_exist(self, tmp_path):
        conn = connect(str(tmp_path / "bot.sqlite3"))
        migrate(conn)
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master"
                                    " WHERE type='table'")
        }
        assert {"post_plans", "publication_parts", "navigation_edits"} <= names
        # unique intent key per (attempt, part)
        cols = {
            row["name"] for row in conn.execute(
                "PRAGMA table_info(publication_parts)")
        }
        assert {"attempt_id", "part_index", "part_count", "state",
                "part_snapshot_json", "channel_message_ids", "permalink",
                "confirmed_at"} <= cols
        conn.close()