"""Shared offline harness for durable poll-loop tests.

A real App (open_offline, no lock, no network) plus fake source/handler and
a fake clock: everything PollLoop touches is injected, so these exercise
the production loop code exactly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from conftest import NOW
from fake_transport import FakeTransport

from poster_bot.app import App, open_offline
from poster_bot.config import Config
from poster_bot.polling import PollFailure, PollLoop
from poster_bot.repo.inbox import InboxRepo
from poster_bot.runtime import Runtime


class FakeClock:
    def __init__(self, start: int = NOW) -> None:
        self.t = start

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds: int) -> int:
        self.t += seconds
        return self.t


def loop_config(tmp_path) -> Config:
    return Config(
        bot_token="DUMMY_TEST_TOKEN_NOT_REAL",
        owner_user_id=111,
        channel_id=-100222,
        db_path=str(tmp_path / "bot.sqlite3"),
        lock_path=str(tmp_path / "bot.lock"),
        timezone="Europe/Moscow",
        slot_start_hour=0,
        slot_end_hour=23,
    )


@dataclass
class FakeSource:
    """Scripted getUpdates: each entry is an Exception to raise or a list of
    updates to hand back (empty list = quiet healthy poll)."""

    script: list[Any] = field(default_factory=list)
    offsets: list[int | None] = field(default_factory=list)
    clock: FakeClock | None = None
    advance_per_failure: int = 0

    async def fetch(self, *, offset: int | None, timeout: int):
        self.offsets.append(offset)
        if not self.script:
            return []
        item = self.script.pop(0)
        if isinstance(item, Exception):
            if self.clock is not None and self.advance_per_failure:
                self.clock.advance(self.advance_per_failure)
            raise item
        return list(item)


class RecordingHandler:
    """Records every handled update; may crash on demand. ``on_handle`` may
    inspect live state DURING handling (e.g. the still-pending inbox row)."""

    def __init__(self, on_handle=None) -> None:
        self.seen: list[int] = []
        self.on_handle = on_handle

    async def handle(self, update) -> None:
        self.seen.append(update.update_id)
        if self.on_handle is not None:
            self.on_handle(update)


def update(update_id: int):
    return SimpleNamespace(update_id=update_id)


@dataclass
class Harness:
    app: App
    inbox: InboxRepo
    transport: FakeTransport
    clock: FakeClock
    source: FakeSource
    handler: RecordingHandler
    loop: PollLoop
    ticks: list[int] = field(default_factory=list)


class _TickRecordingRuntime(Runtime):
    """Delegates to the real Runtime but records the epoch each tick used,
    proving the loop re-reads the clock after its awaits."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.harness: Harness | None = None
        self.fail_next_tick = False

    async def tick(self, *, now_epoch: int):
        if self.harness is not None:
            self.harness.ticks.append(now_epoch)
        if self.fail_next_tick:
            self.fail_next_tick = False
            raise RuntimeError("simulated runtime tick failure")
        return await super().tick(now_epoch=now_epoch)


def build_harness(tmp_path, *, clock=None, source=None, on_handle=None,
                  transport=None) -> Harness:
    clock = clock if clock is not None else FakeClock()
    source = source if source is not None else FakeSource()
    app = open_offline(loop_config(tmp_path))
    # mimic what open_for_run does on a real open: seed the heartbeat so
    # outage detection has a starting point exactly like production
    app.runtime.set_alive(now_epoch=clock.t)
    transport = transport if transport is not None else FakeTransport()
    inbox = InboxRepo(app.conn)
    handler = RecordingHandler(on_handle=on_handle)
    runtime = _TickRecordingRuntime(
        dispatch=app.dispatch,
        notices=app.notices,
        runtime_state=app.runtime,
        transport=transport,
    )
    harness = Harness(app=app, inbox=inbox, transport=transport, clock=clock,
                      source=source, handler=handler, loop=None)
    runtime.harness = harness
    harness.loop = PollLoop(
        source=source,
        handler=handler,
        inbox=inbox,
        runtime=runtime,
        dispatch=app.dispatch,
        notices=app.notices,
        runtime_state=app.runtime,
        journal=app.journal,
        clock=clock,
    )
    if source.clock is None:
        source.clock = clock
    return harness


def run(coro):
    import asyncio

    return asyncio.run(coro)


def advance_healthy(harness: Harness, to_epoch: int, step: int = 60) -> None:
    """Run quiet healthy cycles while the fake clock walks to ``to_epoch``,
    keeping poll health fresh (gap <= tolerance on every step) — exactly
    what a live poller does between minutes."""
    while harness.clock.t < to_epoch:
        harness.clock.t = min(harness.clock.t + step, to_epoch)
        report = run(harness.loop.run_cycle())
        assert report.ok and not report.reconnect, "healthy stepping must not reconnect"