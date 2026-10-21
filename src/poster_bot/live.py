"""Live composition edge: the durable poll loop wired to real aiogram parts.

This module (and ``transport.aiogram_channel``) is the ONLY import path that
loads aiogram; everything it composes — the poll loop, runtime, flow, app —
stays aiogram-free and offline-tested. Composition is dependency-injected:
tests pass a stub ``Bot`` (duck-typed, canned ``get_updates``, never sends)
plus a fake clock, so the wiring itself is exercised without a token or a
socket.

Startup order is a reliability contract (PLAN §6):
1. ``open_for_run`` first — singleton lock, live-config gate, migrations,
   startup sweep, persisted notice. Refusals here never even look at the
   token shape;
2. Bot API token SYNTAX check (aiogram's own validator). A malformed token
   raises ``MalformedTokenError`` naming only the env var — the value is
   never echoed — and the already-acquired lock/DB are released;
3. transport/flow/router/dispatcher composed over the SAME app connection;
4. the loop then polls getUpdates with the durable inbox offset, feeds
   updates through the owner router, and ticks the runtime after every
   successful poll.

Shutdown: SIGINT/SIGTERM set the stop event; every exit path closes the bot
session and the app (flock + DB released) and journals ``poller_stopped``.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass
from typing import Callable, Sequence

from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import Update
from aiogram.utils.token import TokenValidationError, validate_token

from .app import App, open_for_run
from .config import ENV_TOKEN, Config, MalformedTokenError
from .polling import (
    ALLOWED_UPDATES,
    GET_UPDATES_LIMIT,
    PollFailure,
    PollFatal,
    PollLoop,
    UpdateLike,
)
from .repo.inbox import InboxRepo
from .runtime import Runtime
from .transport.aiogram_channel import AiogramChannelTransport, create_bot
from .ui.flow import OwnerFlow
from .ui.handlers import create_ui_router

logger = logging.getLogger(__name__)

#: what run_live hands the CLI once the loop is about to start: ids of
#: owner notices still awaiting delivery (ids are not secrets)
StartedCallback = Callable[[tuple[int, ...]], None]


class AiogramUpdateSource:
    """getUpdates adapter: maps aiogram exceptions onto safe poll codes.

    Only class classifications ever leave this adapter — never Telegram's
    message text, payloads or the token."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def fetch(
        self, *, offset: int | None, timeout: int
    ) -> Sequence[UpdateLike]:
        try:
            return await self._bot.get_updates(
                offset=offset,
                timeout=timeout,
                limit=GET_UPDATES_LIMIT,
                allowed_updates=list(ALLOWED_UPDATES),
            )
        except TelegramUnauthorizedError:
            # token revoked/invalid: backing off would hammer forever
            raise PollFatal("unauthorized") from None
        except Exception as exc:
            match exc:
                case TimeoutError():
                    raise PollFailure("timeout") from None
                case TelegramNetworkError() | TelegramServerError():
                    raise PollFailure("transport_error") from None
                case TelegramAPIError():
                    raise PollFailure("api_error") from None
                case _:
                    # unknown adapter-level failure: class name is a safe
                    # diagnostic code, never the message text
                    raise PollFailure(type(exc).__name__) from None


class DispatcherUpdateHandler:
    """Feeds one update through the owner router.

    Exceptions propagate to the poll loop, which leaves the inbox row
    pending so the update is re-delivered — never marked done, never lost.
    Delivery is at-least-once: when a crash lands after the handler's
    effects have committed but before the row completes, the replay runs
    the handler again and its durable effects can duplicate; no exactly-once
    guarantee exists anywhere in this path.

    Log hygiene: only ``feed_update`` is ever called — deliberately never
    ``_process_update``/``feed_webhook_update``, whose error paths log the
    exception text with a traceback at ERROR level. aiogram's update-type
    resolver RuntimeWarning-dumps the FULL update JSON (aiogram models keep
    unknown fields), so updates without a resolvable event type are settled
    here before aiogram sees them: they carry no handler work, complete
    their inbox row, and are logged with ids and safe codes only."""

    def __init__(self, dispatcher: Dispatcher, bot: Bot) -> None:
        self._dispatcher = dispatcher
        self._bot = bot

    async def handle(self, update: UpdateLike) -> None:
        try:
            update.event_type  # type: ignore[attr-defined]
        except AttributeError:
            pass  # non-aiogram update object (offline fakes): feed as-is
        except LookupError:
            # aiogram would warn-dump the whole update JSON: owner content
            # must never reach stderr or logs. Nothing typed, nothing to do.
            logger.warning(
                "update %d skipped: no known event type", update.update_id
            )
            return
        await self._dispatcher.feed_update(self._bot, update)  # type: ignore[arg-type]


@dataclass
class Live:
    """Everything the poller owns for one process, with one close()."""

    app: App
    bot: Bot
    dispatcher: Dispatcher
    transport: AiogramChannelTransport
    inbox: InboxRepo
    runtime: Runtime
    loop: PollLoop

    async def close(self) -> None:
        try:
            await self.bot.session.close()
        except Exception as exc:  # release everything below regardless
            logger.warning("bot session close failed: %s", type(exc).__name__)
        self.app.close()


def compose_live(
    config: Config,
    *,
    clock: Callable[[], int],
    bot: Bot | None = None,
    now_epoch: int | None = None,
) -> Live:
    """Build the live object graph over a freshly opened App.

    Any failure after ``open_for_run`` releases the lock and DB before
    propagating. ``bot`` is injected only by offline tests."""
    app = open_for_run(config, now_epoch=now_epoch)
    try:
        try:
            validate_token(config.bot_token)
        except TokenValidationError:
            raise MalformedTokenError(ENV_TOKEN) from None
        the_bot = bot if bot is not None else create_bot(config)
        # composed directly to keep the whole live graph visible in one place;
        # build_transport(config, bot=...) is the identical wiring for callers
        transport = AiogramChannelTransport(
            the_bot,
            channel_id=config.channel_id,
            owner_id=config.owner_user_id,
        )
        flow = OwnerFlow(
            conn=app.conn,
            settings=app.settings,
            drafts=app.drafts,
            publications=app.publications,
            dispatch=app.dispatch,
            manual_times=app.manual_times,
            journal=app.journal,
            preview=transport,
            channel_id=config.channel_id,
        )
        router = create_ui_router(
            flow, owner_id=config.owner_user_id, clock=clock
        )
        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        inbox = InboxRepo(app.conn)
        runtime = Runtime.for_app(app, transport)
        loop = PollLoop(
            source=AiogramUpdateSource(the_bot),
            handler=DispatcherUpdateHandler(dispatcher, the_bot),
            inbox=inbox,
            runtime=runtime,
            dispatch=app.dispatch,
            notices=app.notices,
            runtime_state=app.runtime,
            journal=app.journal,
            clock=clock,
        )
        return Live(
            app=app,
            bot=the_bot,
            dispatcher=dispatcher,
            transport=transport,
            inbox=inbox,
            runtime=runtime,
            loop=loop,
        )
    except BaseException:
        app.close()
        raise


async def run_live(
    config: Config,
    *,
    clock: Callable[[], int],
    bot: Bot | None = None,
    stop_event: asyncio.Event | None = None,
    install_signals: bool = True,
    now_epoch: int | None = None,
    on_started: StartedCallback | None = None,
) -> None:
    """Poll until ``stop_event`` fires (or a signal sets it), then release
    everything. Raises ``PollFatal`` after clean cleanup for the CLI to map
    to an exit code; never retries a fatal condition."""
    live = compose_live(config, clock=clock, bot=bot, now_epoch=now_epoch)
    stop = stop_event if stop_event is not None else asyncio.Event()
    installed: list[signal.Signals] = []
    if install_signals:
        running = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                running.add_signal_handler(sig, stop.set)
                installed.append(sig)
            except (NotImplementedError, RuntimeError):
                pass  # platform without signal handlers: stop_event still works
    fatal: PollFatal | None = None
    try:
        if on_started is not None:
            on_started(tuple(notice.id for notice in live.app.notices.pending()))
        while not stop.is_set():
            try:
                report = await live.loop.run_cycle()
            except PollFatal as exc:
                fatal = exc
                break
            if report.sleep_seconds > 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=report.sleep_seconds)
                except asyncio.TimeoutError:
                    pass
            else:
                # always give the event loop one turn per cycle: signal
                # callbacks and other loop tasks must not be starved by a
                # fast healthy cycle
                await asyncio.sleep(0)
        live.app.journal.record("poller_stopped", now_epoch=clock())
        if fatal is not None:
            logger.warning("poller stopped by fatal poll: code=%s", fatal.code)
            raise fatal
    finally:
        for sig in installed:
            try:
                asyncio.get_running_loop().remove_signal_handler(sig)
            except Exception as exc:  # loop may already be closing on cancel
                logger.warning("signal handler removal failed: %s", type(exc).__name__)
        await live.close()