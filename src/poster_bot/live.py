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
3. channel TARGET resolution: a numeric id is used as configured (no call);
   a configured ``@channel-username`` is resolved via getChat to its stable
   numeric chat id, accepted only as a ``channel``-type chat with a negative
   numeric id. Any refusal raises ``ChannelResolutionError`` (env var and
   safe reason only) BEFORE any transport/flow is composed or any send is
   possible, and releases the bot session plus the lock/DB;
4. transport/flow/router/dispatcher composed over the SAME app connection,
   all bound to the ONE resolved numeric destination (public username is
   carried only for building correct t.me links);
5. the loop then polls getUpdates with the durable inbox offset, feeds
   updates through the owner router, and ticks the runtime after every
   successful poll.

Shutdown: SIGINT/SIGTERM set the stop event; every exit path closes the bot
session and the app (flock + DB released) and journals ``poller_stopped``.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import signal
import socket
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import Update
from aiogram.utils.token import TokenValidationError, validate_token
from aiohttp import ClientConnectorError

from .app import App, open_for_run
from .config import (
    ENV_CHANNEL,
    ENV_TOKEN,
    ChannelResolutionError,
    Config,
    MalformedTokenError,
)
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
from .ui.dashboard_presentation import DashboardPresentation

logger = logging.getLogger(__name__)

#: what run_live hands the CLI once the loop is about to start: ids of
#: owner notices still awaiting delivery (ids are not secrets)
StartedCallback = Callable[[tuple[int, ...]], None]


@dataclass(frozen=True)
class ResolvedChannel:
    """The ONE destination every live component shares.

    ``channel_id`` is always the stable numeric chat id publishes/preview
    copies target; ``username`` (bare, no leading '@') is carried only to
    build correct public t.me links and is never a send destination."""

    channel_id: int
    username: str | None = None


async def resolve_channel(bot: Bot, *, channel_id: int | str) -> ResolvedChannel:
    """Resolve the configured target to its numeric channel id.

    Numeric targets are returned as-is (existing behavior: no extra call).
    A ``@username`` is resolved with one getChat and accepted only as a
    ``channel``-type chat with a negative numeric id — a group or private
    chat behind that username is refused, never published into. Failures
    raise ``ChannelResolutionError`` naming only the env var and a safe
    reason code: Telegram's error text, chat payloads and the token never
    leave this function."""
    if isinstance(channel_id, str):
        try:
            chat = await bot.get_chat(chat_id=channel_id)
        except (asyncio.TimeoutError, TelegramNetworkError, TelegramServerError):
            raise ChannelResolutionError(
                ENV_CHANNEL, "Telegram not reachable"
            ) from None
        except TelegramAPIError:
            raise ChannelResolutionError(
                ENV_CHANNEL, "chat is not accessible to this bot"
            ) from None
        except Exception as exc:  # unknown adapter-level failure: class name
            raise ChannelResolutionError(  # is a safe code, never the message
                ENV_CHANNEL, type(exc).__name__
            ) from None
        if (
            getattr(chat, "type", None) != "channel"
            or not isinstance(getattr(chat, "id", None), int)
            or chat.id >= 0
        ):
            raise ChannelResolutionError(
                ENV_CHANNEL, "target is not a channel chat"
            ) from None
        return ResolvedChannel(channel_id=chat.id, username=channel_id[1:])
    return ResolvedChannel(channel_id=channel_id)


def _network_failure_category(exc: BaseException) -> str:
    """Inspect types/errno only; aiogram chains its original aiohttp cause.

    Never parse/stringify exceptions: aiohttp messages can contain token URLs.
    Bound traversal, including when malformed cause chains contain a cycle.
    """
    causes = []
    current: BaseException | None = exc
    for _ in range(8):
        if current is None or any(current is item for item in causes):
            break
        causes.append(current)
        current = current.__cause__ or current.__context__
    if any(isinstance(item, (TimeoutError, asyncio.TimeoutError)) for item in causes):
        return "timeout"
    if any(isinstance(item, socket.gaierror) for item in causes):
        return "dns"
    if any(
        isinstance(item, (ClientConnectorError, ConnectionRefusedError))
        or (isinstance(item, OSError) and item.errno in {
            errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH,
        })
        for item in causes
    ):
        return "connect"
    return "network"


class AiogramUpdateSource:
    """getUpdates adapter: maps aiogram exceptions onto safe poll codes.

    Only class classifications ever leave this adapter — never Telegram's
    message text, payloads or the token."""

    def __init__(
        self, bot: Bot, *, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._bot = bot
        self._monotonic = monotonic

    async def fetch(
        self, *, offset: int | None, timeout: int
    ) -> Sequence[UpdateLike]:
        started = self._monotonic()
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
            duration_ms = max(0, int((self._monotonic() - started) * 1000))
            retry_after = None
            match exc:
                case TelegramRetryAfter():
                    code, category, class_name = (
                        "rate_limit", "rate_limit", "TelegramRetryAfter"
                    )
                    retry_after = exc.retry_after
                case TimeoutError() | asyncio.TimeoutError():
                    code, category, class_name = "timeout", "timeout", "TimeoutError"
                case TelegramNetworkError():
                    code, category, class_name = (
                        "transport_error", _network_failure_category(exc),
                        "TelegramNetworkError",
                    )
                case TelegramServerError():
                    code, category, class_name = (
                        "transport_error", "server", "TelegramServerError"
                    )
                case TelegramAPIError():
                    code, category, class_name = "api_error", "api", "TelegramAPIError"
                case _:
                    code, category, class_name = "unclassified", "unknown", "Exception"
            raise PollFailure(
                code, failure_class=class_name, category=category,
                request_duration_ms=duration_ms, retry_after=retry_after,
            ) from None


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


async def compose_live(
    config: Config,
    *,
    clock: Callable[[], int],
    bot: Bot | None = None,
    now_epoch: int | None = None,
) -> Live:
    """Build the live object graph over a freshly opened App.

    Any failure after ``open_for_run`` releases the lock, DB and (once
    created) the bot session before propagating. ``bot`` is injected only by
    offline tests."""
    app = open_for_run(config, now_epoch=now_epoch)
    try:
        try:
            validate_token(config.bot_token)
        except TokenValidationError:
            raise MalformedTokenError(ENV_TOKEN) from None
        the_bot = bot if bot is not None else create_bot(config)
        try:
            resolved = await resolve_channel(the_bot, channel_id=config.channel_id)
        except BaseException:
            # no Live exists yet to close the session: release it here so a
            # refused resolution never strands an HTTP client
            try:
                await the_bot.session.close()
            except Exception as exc:  # release the app below regardless
                logger.warning("bot session close failed: %s", type(exc).__name__)
            raise
        # composed directly to keep the whole live graph visible in one place;
        # every component shares the ONE resolved numeric destination
        transport = AiogramChannelTransport(
            the_bot,
            channel_id=resolved.channel_id,
            owner_id=config.owner_user_id,
            channel_username=resolved.username,
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
            channel_id=resolved.channel_id,
            channel_username=resolved.username,
        )
        dashboard = DashboardPresentation(flow, owner_id=config.owner_user_id)
        router = create_ui_router(
            flow, owner_id=config.owner_user_id, clock=clock, dashboard=dashboard,
        )
        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        inbox = InboxRepo(app.conn)
        async def refresh_dashboard(now: int) -> None:
            await dashboard.refresh(the_bot, now=now)

        runtime = Runtime.for_app(app, transport, refresh_dashboard=refresh_dashboard)
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
    live = await compose_live(config, clock=clock, bot=bot, now_epoch=now_epoch)
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
