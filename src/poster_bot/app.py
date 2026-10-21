"""Composition root: wires config, storage and services behind one entry.

`open_offline` never touches the live gate or network. `open_for_run` is the
live entry: singleton lock -> live config gate -> storage -> startup recovery
sweep -> persisted downtime notice (when an outage gap or a schedule parked
during one is detected from the stored heartbeat).

Reliability of the recovery notice (PLAN §2): the notice is persisted to the
owner_notices outbox inside open_for_run, BEFORE this function returns and
before any delivery attempt. A crash later can therefore never lose it; the
live runtime delivers pending notices and marks them sent. Any failure after
the lock was acquired releases the lock and closes the connection, so a
refused or failed open never leaks the singleton lock or a DB handle.

Telegram UI (phase 2) consumes this App object; no aiogram import lives on
this import path (enforced by tests).
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config, validate_for_live
from .db import connect, migrate, utc_now
from .journal import Journal
from .repo.dispatch import DispatchService
from .repo.drafts import DraftStore
from .repo.manual_times import ManualTimesRepo
from .repo.notifications import OwnerNoticeStore
from .repo.publications import PublicationsService
from .repo.runtime_state import RuntimeState
from .repo.settings import SettingsRepo
from .singleton import ProcessLock

#: a restart gap of at most this is treated as a routine restart, not an
#: outage; longer gaps produce the persisted recovery notice to the owner
OUTAGE_TOLERANCE_SECONDS = 120


@dataclass
class App:
    config: Config
    conn: object
    lock: ProcessLock | None
    journal: Journal
    drafts: DraftStore
    publications: PublicationsService
    dispatch: DispatchService
    settings: SettingsRepo
    manual_times: ManualTimesRepo
    notices: OwnerNoticeStore
    runtime: RuntimeState

    def close(self) -> None:
        self.conn.close()
        if self.lock is not None:
            self.lock.release()


def _build(config: Config, conn, lock: ProcessLock | None) -> App:
    journal = Journal(conn)
    settings = SettingsRepo(conn)
    settings.seed_defaults(config)
    return App(
        config=config,
        conn=conn,
        lock=lock,
        journal=journal,
        drafts=DraftStore(conn),
        publications=PublicationsService(conn, journal),
        dispatch=DispatchService(conn, journal),
        settings=settings,
        manual_times=ManualTimesRepo(conn),
        notices=OwnerNoticeStore(conn, journal),
        runtime=RuntimeState(conn),
    )


def open_offline(config: Config, *, use_lock: bool = False) -> App:
    """Open storage without any live-mode gate or network use."""
    lock = None
    if use_lock:
        lock = ProcessLock(config.lock_path)
        lock.acquire()
    try:
        conn = connect(config.db_path)
        migrate(conn)
        return _build(config, conn, lock)
    except BaseException:
        if lock is not None:
            lock.release()
        raise


def open_for_run(config: Config, *, now_epoch: int | None = None) -> App:
    """Live run gate: singleton lock first (a second instance is refused no
    matter what the environment says), then the live-config gate, migrations,
    one startup recovery sweep and the persisted recovery notice when the
    stored heartbeat shows an outage gap. Never sends anything to the channel
    here — past-due work is only marked, and the notice goes out via the
    outbox once the runtime is up."""
    now = int(utc_now().timestamp()) if now_epoch is None else now_epoch
    lock = ProcessLock(config.lock_path)
    lock.acquire()
    conn = None
    try:
        validate_for_live(config)
        conn = connect(config.db_path)
        migrate(conn)
        app = _build(config, conn, lock)
    except BaseException:
        if conn is not None:
            conn.close()
        lock.release()
        raise
    try:
        last_alive = app.runtime.last_alive()
        # The last stored heartbeat is the outage start: anything scheduled
        # inside the gap (even a minute still open at reopen) is parked —
        # a minute that began while the bot was down must never be replayed.
        result = app.dispatch.recovery_sweep(
            now_epoch=now, outage_start_epoch=last_alive
        )
        if (
            last_alive is not None
            and last_alive < now
            and (
                now - last_alive > OUTAGE_TOLERANCE_SECONDS
                or result.anything_changed
            )
        ):
            affected = tuple(
                sorted(set(result.uncertain_publication_ids) | set(result.missed_publication_ids))
            )
            app.notices.queue_recovery_downtime(
                start_epoch=last_alive,
                end_epoch=now,
                affected_ids=affected,
                now_epoch=now,
            )
        app.runtime.set_alive(now_epoch=now)
    except BaseException:
        app.close()
        raise
    return app