from __future__ import annotations

import os
import stat
import subprocess
import sys
import time

import pytest

from poster_bot.singleton import AlreadyRunningError, ProcessLock


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def permissive_umask():
    old = os.umask(0o000)  # worst case: makedirs/open defaults leak modes
    try:
        yield
    finally:
        os.umask(old)

HOLDER_CODE = """
import time
from poster_bot.singleton import ProcessLock
lock = ProcessLock({path!r})
lock.acquire()
print("locked", flush=True)
time.sleep({hold})
"""


def _wait_until_held(lock_path: str, timeout: float = 10.0) -> None:
    """Poll from a second ProcessLock instance until the holder has the flock."""
    deadline = time.monotonic() + timeout
    probe = ProcessLock(lock_path)
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            probe.acquire()
            probe.release()
        except AlreadyRunningError:
            return
        except Exception as exc:  # pragma: no cover - diagnostics only
            last_error = exc
        time.sleep(0.05)
    raise AssertionError(f"lock never held by holder: {last_error}")


class TestSameProcess:
    def test_second_instance_refused(self, tmp_path):
        path = str(tmp_path / "bot.lock")
        first = ProcessLock(path)
        first.acquire()
        other = ProcessLock(path)
        with pytest.raises(AlreadyRunningError):
            other.acquire()
        first.release()
        other.acquire()  # path is reusable after release
        other.release()

    def test_release_is_idempotent(self, tmp_path):
        lock = ProcessLock(str(tmp_path / "bot.lock"))
        lock.acquire()
        lock.release()
        lock.release()


class TestLockPathPermissions:
    """The lock is taken BEFORE db.connect runs its private-directory logic:
    freshly created lock parents must already be 0700 (the 0755 default left
    the later DB privacy code with an EXISTING directory it never re-modes),
    and nothing that already exists is ever chmod'ed."""

    def test_new_dir_chain_is_0700_and_lock_is_0600(self, tmp_path,
                                                    permissive_umask):
        nested = tmp_path / "data" / "run"
        lock = ProcessLock(str(nested / "bot.lock"))
        lock.acquire()
        try:
            assert _mode(str(tmp_path / "data")) == 0o700
            assert _mode(str(nested)) == 0o700
            assert _mode(str(nested / "bot.lock")) == 0o600
        finally:
            lock.release()

    def test_existing_parent_directory_is_not_chmodded(self, tmp_path,
                                                       permissive_umask):
        parent = tmp_path / "shared"
        parent.mkdir()
        os.chmod(parent, 0o755)  # the user's own choice of mode
        lock = ProcessLock(str(parent / "bot.lock"))
        lock.acquire()
        try:
            assert _mode(str(parent)) == 0o755  # untouched
        finally:
            lock.release()

    def test_existing_lock_file_mode_is_preserved(self, tmp_path,
                                                  permissive_umask):
        path = tmp_path / "bot.lock"
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o640)
        os.close(fd)
        lock = ProcessLock(str(path))
        lock.acquire()
        try:
            assert _mode(str(path)) == 0o640  # never re-mode a user's file
        finally:
            lock.release()


class TestSubprocessSingleton:
    def test_run_refused_while_holder_alive(self, tmp_path):
        lock_path = str(tmp_path / "bot.lock")
        db_path = str(tmp_path / "bot.sqlite3")
        env_file = tmp_path / "env"
        env_file.write_text(
            f"POSTER_BOT_DB_PATH={db_path}\nPOSTER_BOT_LOCK_PATH={lock_path}\n"
        )
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                HOLDER_CODE.format(path=lock_path, hold=30),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_until_held(lock_path)
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "poster_bot",
                    "--env-file",
                    str(env_file),
                    "run",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert run.returncode == 4, run.stderr
            assert "refused" in run.stderr
        finally:
            holder.terminate()
            holder.wait(timeout=10)
        # lock released: run now passes the singleton gate and stops at the
        # live-config gate (dummy env) — proving lock is no longer the blocker
        run_after = subprocess.run(
            [
                sys.executable,
                "-m",
                "poster_bot",
                "--env-file",
                str(env_file),
                "run",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert run_after.returncode == 3, run_after.stderr