"""Singleton process lock via flock: at most one bot process per lock file."""
from __future__ import annotations

import errno
import fcntl
import os


class AlreadyRunningError(RuntimeError):
    """Another process holds the singleton lock."""


def _ensure_private_dir(path: str) -> None:
    """Create ``path`` (and missing ancestors) with mode 0700.

    Same privacy contract as the database directory in ``db.connect``: the
    lock is acquired BEFORE the database is opened, so if this call created
    the data directory with the ``os.makedirs`` default (0o777 & ~umask,
    typically world-readable 0755), the later database privacy logic would
    see an EXISTING directory and never re-mode it -- the owner's private
    post text would live in a world-readable directory. Only directories
    this call actually creates are chmod'ed to 0700 (umask-proof); anything
    that already exists is left exactly as the user set it."""
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


class ProcessLock:
    """Non-blocking exclusive flock on a file path.

    The lock is owned by the OS file description; a crashed process releases
    it automatically when its kernel-side resources go away.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def path(self) -> str:
        return self._path

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError("lock already acquired")
        parent = os.path.dirname(os.path.abspath(self._path))
        _ensure_private_dir(parent)
        # a pre-existing lock file keeps the mode its owner chose; a newly
        # created one is pinned to 0600 (os.open's mode is umask-masked, so
        # a newly created file gets an explicit fchmod)
        existed_before = os.path.exists(self._path)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        if not existed_before:
            os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise AlreadyRunningError(
                    "another poster-bot process holds the singleton lock"
                ) from None
            raise
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()