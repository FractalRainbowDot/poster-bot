"""Restrictive filesystem permissions for freshly created poster-bot data.

The database stores the owner's private post text. These tests drive the
real ``poster_bot.db.connect`` under a deliberately PERMISSIVE umask (0) to
prove the code sets modes itself: new database files and WAL sidecars are
0600, newly created directories are 0700, and nothing that already existed
(user paths, existing databases) is ever chmod'ed.
"""
from __future__ import annotations

import os
import stat

import pytest

from poster_bot.app import open_for_run
from poster_bot.config import (
    ENV_CHANNEL,
    ENV_OWNER,
    ENV_TOKEN,
    load_config,
)
from poster_bot.db import connect, migrate

SENTINEL = "PRIVATE POST TEXT"
#: any fixed epoch: open_for_run never talks to the network with this config
RUN_EPOCH = 1_790_000_000


@pytest.fixture
def permissive_umask():
    old = os.umask(0o000)  # worst case: sqlite defaults would be 0666
    try:
        yield
    finally:
        os.umask(old)


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class TestFreshDataRestrictions:
    def test_new_db_wal_and_shm_are_owner_only(self, tmp_path, permissive_umask):
        db = str(tmp_path / "data" / "bot.sqlite3")
        conn = connect(db)
        try:
            migrate(conn)
            with conn:
                conn.execute("CREATE TABLE probe(x TEXT)")
                conn.execute("INSERT INTO probe VALUES (?)", (SENTINEL,))
            assert _mode(db) == 0o600
            assert os.path.exists(db + "-wal"), "WAL mode must be active"
            assert _mode(db + "-wal") == 0o600
            assert _mode(db + "-shm") == 0o600
        finally:
            conn.close()

    def test_sidecars_stay_private_across_reconnect(self, tmp_path,
                                                    permissive_umask):
        db = str(tmp_path / "data" / "bot.sqlite3")
        first = connect(db)
        migrate(first)
        with first:
            first.execute("CREATE TABLE probe(x TEXT)")
            first.execute("INSERT INTO probe VALUES (?)", (SENTINEL,))
        first.close()  # clean close checkpoints and removes sidecars
        second = connect(db)
        try:
            with second:
                second.execute("INSERT INTO probe VALUES (?)", ("again",))
            assert _mode(db) == 0o600
            assert _mode(db + "-wal") == 0o600
            assert _mode(db + "-shm") == 0o600
        finally:
            second.close()

    def test_new_directory_chain_is_0700(self, tmp_path, permissive_umask):
        nested = tmp_path / "a" / "b" / "c"
        conn = connect(str(nested / "bot.sqlite3"))
        try:
            for created in (tmp_path / "a", tmp_path / "a" / "b", nested):
                assert _mode(str(created)) == 0o700
        finally:
            conn.close()


class TestExistingPathsUntouched:
    def test_existing_parent_directory_is_not_chmodded(self, tmp_path,
                                                       permissive_umask):
        parent = tmp_path / "shared"
        parent.mkdir()
        os.chmod(parent, 0o755)  # the user's own choice of mode
        conn = connect(str(parent / "bot.sqlite3"))
        try:
            migrate(conn)
            assert _mode(str(parent)) == 0o755  # left exactly as the user set
        finally:
            conn.close()

    def test_existing_database_file_mode_is_preserved(self, tmp_path,
                                                      permissive_umask):
        db = tmp_path / "user.sqlite3"
        fd = os.open(str(db), os.O_CREAT | os.O_WRONLY, 0o644)
        os.close(fd)
        assert _mode(str(db)) == 0o644
        conn = connect(str(db))
        try:
            migrate(conn)  # adopts the pre-existing (empty) database
            assert _mode(str(db)) == 0o644  # never re-mode a user's file
        finally:
            conn.close()

    def test_existing_sidecar_modes_are_preserved(self, tmp_path,
                                                  permissive_umask):
        # sidecars left over from before (zero-length WAL is a valid empty
        # WAL): connect must reuse them, not replace or re-mode them
        db = str(tmp_path / "bot.sqlite3")
        for path in (db, db + "-wal", db + "-shm"):
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o640)
            os.close(fd)
        conn = connect(db)
        try:
            migrate(conn)
            with conn:
                conn.execute("CREATE TABLE probe(x)")
            assert _mode(db) == 0o640
            assert _mode(db + "-wal") == 0o640
            assert _mode(db + "-shm") == 0o640
        finally:
            conn.close()


class TestOpenForRunFreshDataDir:
    """Acceptance: on a live open with DEFAULT relative paths ('data/...'),
    the singleton lock creates the data directory BEFORE db.connect's
    private-dir logic sees it. If the lock used the makedirs default (0755),
    connect would treat the world-readable directory as pre-existing and
    never re-mode it -- so the newly created chain must come out 0700 from
    the lock itself, and the database must land at 0600."""

    def test_default_data_dir_created_by_open_for_run_is_private(
        self, tmp_path, permissive_umask, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)  # default paths are relative to cwd
        config = load_config(
            {
                ENV_TOKEN: "DUMMY_TEST_TOKEN_NOT_REAL",
                ENV_OWNER: "111",
                ENV_CHANNEL: "-100222",
            }
        )
        assert config.db_path == "data/poster_bot.sqlite3"
        assert config.lock_path == "data/poster_bot.lock"
        app = open_for_run(config, now_epoch=RUN_EPOCH)
        try:
            assert _mode("data") == 0o700  # created by the lock, umask-proof
            assert _mode("data/poster_bot.lock") == 0o600
            assert _mode("data/poster_bot.sqlite3") == 0o600
            assert (tmp_path / "data" / "poster_bot.sqlite3").exists()
        finally:
            app.close()

    def test_open_for_run_does_not_touch_existing_data_dir_mode(
        self, tmp_path, permissive_umask, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        data = tmp_path / "data"
        data.mkdir()
        os.chmod(data, 0o755)  # the user's own choice of mode
        config = load_config(
            {
                ENV_TOKEN: "DUMMY_TEST_TOKEN_NOT_REAL",
                ENV_OWNER: "111",
                ENV_CHANNEL: "-100222",
            }
        )
        app = open_for_run(config, now_epoch=RUN_EPOCH)
        try:
            assert _mode("data") == 0o755  # existing directories stay as set
            assert _mode("data/poster_bot.sqlite3") == 0o600  # new files pin
        finally:
            app.close()