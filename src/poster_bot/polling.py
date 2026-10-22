"""Durable getUpdates polling loop (aiogram-free core; PLAN §6).

Ordering per cycle — the whole reliability story lives in this file:

1. one long-poll ``fetch`` with the DURABLE offset from ``InboxRepo.
   safe_offset()``: a pending inbox row pins the offset, so Telegram can
   never prune an update this process still owes (no silent backlog drop);
2. a successful poll re-confirms connectivity: any previous "connectivity
   unsafe" state (ambiguous channel send) clears only here;
3. a reconnecting cycle (any consecutive poll failure just ended, or a gap
   since the last successful poll beyond the outage tolerance) runs the
   recovery sweep with the outage start as ``outage_start_epoch`` — posts
   that fell due during the outage are parked even if their minute is
   still open (a minute that began while the bot was down is missed,
   never auto-caught-up) — journals ``outage_detected`` and persists one
   downtime notice (interval + affected ids) through the outbox;
4. health recorded only for a SUCCESSFUL poll (failed polls leave the
   heartbeat frozen so the next reconnect sees the real gap);
5. each update: ``inbox.begin`` BEFORE the handler (DONE rows are dedup
   skips), handler effects commit, THEN ``inbox.complete`` — mark-after-
   persist. A handler exception leaves the row pending for re-delivery;
   the loop never stops for one bad update and never marks it done;
6. the runtime tick runs ONLY after a successful poll, at a FRESHLY read
   clock (time elapsed across every await above; a stale "now" could claim
   a publication whose window already closed);
7. an ambiguous channel send (timeout/transport error) sets connectivity
   unsafe until the next successful poll proves connectivity again.

No sleep happens here: ``CycleReport.sleep_seconds`` carries the backoff
(1s doubling per consecutive failure, capped) and the caller owns waiting,
so a stop signal can cut the wait short. Exceptions are classified into
safe codes only (``PollFailure`` retryable, ``PollFatal`` aborts); logging
carries exception CLASS names — never messages, payloads or tokens.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from .app import OUTAGE_TOLERANCE_SECONDS
from .journal import Journal
from .repo.dispatch import DispatchService
from .repo.inbox import InboxRepo, InboxStatus
from .repo.notifications import OwnerNoticeStore
from .repo.runtime_state import RuntimeState
from .runtime import Runtime

logger = logging.getLogger(__name__)

#: long-poll wait per cycle (clamped by callers to 1..5; aiogram passes it
#: straight to getUpdates ``timeout``)
POLL_TIMEOUT_SECONDS = 2
#: getUpdates ``limit``: never let Telegram cap the batch below what a
#: healthy cycle can handle sequentially
GET_UPDATES_LIMIT = 100
#: only these update types are ever requested (PLAN scope)
ALLOWED_UPDATES = ("message", "callback_query")

BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
# Read-only polling must remain responsive after connectivity returns.
# 1, 2, 4 seconds bounds extra interactive latency without a tight retry loop.
TRANSIENT_BACKOFF_MAX_SECONDS = 4.0

#: ambiguous channel outcomes that mean "connectivity is not to be trusted"
_UNSAFE_SEND_CODES = frozenset({"timeout", "transport_error"})


class PollFailure(Exception):
    """Retryable getUpdates problem (timeout, network, transient API error).
    Carries safe classifications/numeric timing only, never API message text.
    ``retry_after`` is Telegram's minimum wait on read-only getUpdates (429)."""

    def __init__(
        self, code: str, *, failure_class: str = "PollFailure",
        category: str = "unknown", request_duration_ms: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.failure_class = failure_class
        self.category = category
        self.request_duration_ms = request_duration_ms
        self.retry_after = retry_after


class PollFatal(Exception):
    """Unrecoverable for this process: bad/revoked token etc. The runner
    must stop, not keep backing off against a permanent rejection."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class UpdateLike(Protocol):
    """Only ``update_id`` is needed to schedule durable handling; aiogram's
    Update satisfies this and so do test fakes."""

    @property
    def update_id(self) -> int: ...


class UpdateSource(Protocol):
    """Long-poll port. Must return every update with id >= ``offset`` and
    raise only ``PollFailure``/``PollFatal`` for transport problems."""

    async def fetch(
        self, *, offset: int | None, timeout: int
    ) -> Sequence[UpdateLike]: ...


class UpdateHandler(Protocol):
    """Applies one update's durable effects. Exceptions leave the inbox row
    pending; the handler itself must be idempotent for re-delivered ids."""

    async def handle(self, update: UpdateLike) -> None: ...


Clock = Callable[[], int]


@dataclass(frozen=True)
class CycleReport:
    """One cycle's outcome for the runner and for tests."""

    ok: bool
    failure_code: str | None = None
    failure_class: str | None = None
    failure_category: str | None = None
    request_duration_ms: int | None = None
    fetched: int = 0
    handled: int = 0
    skipped_done: int = 0
    handler_failures: int = 0
    pending_after: tuple[int, ...] = ()
    reconnect: bool = False
    outage_start: int | None = None
    downtime_seconds: int | None = None
    parked_uncertain: tuple[int, ...] = ()
    parked_missed: tuple[int, ...] = ()
    notice_queued: bool = False
    ticked: bool = False
    tick_failed: bool = False
    unsafe: bool = False
    sleep_seconds: float = 0.0


def _backoff_seconds(consecutive_failures: int, *, transient: bool = False) -> float:
    return min(
        BACKOFF_BASE_SECONDS * (2 ** min(consecutive_failures - 1, 5)),
        TRANSIENT_BACKOFF_MAX_SECONDS if transient else BACKOFF_MAX_SECONDS,
    )


class PollLoop:
    """Sequential durable poll cycle around the injected pieces.

    Everything time- or network-related is injected (clock, source,
    handler): the loop itself is pure orchestration and fully testable
    offline. One instance drives one process; the consecutive-failure and
    unsafe counters are in-process state by design (durable facts live in
    RuntimeState / the inbox).
    """

    def __init__(
        self,
        *,
        source: UpdateSource,
        handler: UpdateHandler,
        inbox: InboxRepo,
        runtime: Runtime,
        dispatch: DispatchService,
        notices: OwnerNoticeStore,
        runtime_state: RuntimeState,
        journal: Journal,
        clock: Clock,
    ) -> None:
        self._source = source
        self._handler = handler
        self._inbox = inbox
        self._runtime = runtime
        self._dispatch = dispatch
        self._notices = notices
        self._runtime_state = runtime_state
        self._journal = journal
        self._clock = clock
        self._consecutive_failures = 0
        self._unsafe = False

    @property
    def unsafe(self) -> bool:
        """True from an ambiguous channel send until the next poll success."""
        return self._unsafe

    async def run_cycle(self) -> CycleReport:
        """One full cycle; raises only ``PollFatal`` (caller must stop)."""
        prev_poll = self._runtime_state.last_poll()
        offset = self._inbox.safe_offset()
        try:
            updates = await self._source.fetch(
                offset=offset, timeout=POLL_TIMEOUT_SECONDS
            )
        except PollFatal:
            raise
        except PollFailure as exc:
            self._consecutive_failures += 1
            return self._failure_report(
                exc.code, exc.failure_class, category=exc.category,
                duration_ms=exc.request_duration_ms, retry_after=exc.retry_after,
            )
        except Exception as exc:  # adapter violated its contract
            self._consecutive_failures += 1
            return self._failure_report("unclassified", type(exc).__name__)

        # --- poll success: connectivity proven again -------------------------
        # captured BEFORE the reset: ending any failure streak IS the reconnect
        ended_failure_streak = self._consecutive_failures > 0
        self._consecutive_failures = 0
        self._unsafe = False
        now = self._clock()
        outage_start = prev_poll
        if outage_start is None:
            outage_start = self._runtime_state.last_alive()
        reconnect = self._reconnect_detected(
            prev_poll, now, ended_failure_streak=ended_failure_streak
        )
        parked_uncertain: tuple[int, ...] = ()
        parked_missed: tuple[int, ...] = ()
        downtime_seconds: int | None = None
        notice_queued = False
        if reconnect:
            if outage_start is not None and now > outage_start:
                downtime_seconds = now - outage_start
                self._journal.record(
                    "outage_detected",
                    now_epoch=now,
                    downtime_seconds=downtime_seconds,
                )
            result = self._dispatch.recovery_sweep(
                now_epoch=now,
                outage_start_epoch=(
                    outage_start if outage_start is not None else now
                ),
            )
            parked_uncertain = result.uncertain_publication_ids
            parked_missed = result.missed_publication_ids
            if (
                outage_start is not None
                and now > outage_start
                and (
                    now - outage_start > OUTAGE_TOLERANCE_SECONDS
                    or result.anything_changed
                )
            ):
                affected = tuple(
                    sorted(set(parked_uncertain) | set(parked_missed))
                )
                self._notices.queue_recovery_downtime(
                    start_epoch=outage_start,
                    end_epoch=now,
                    affected_ids=affected,
                    now_epoch=now,
                )
                notice_queued = True
        self._runtime.record_poll_success(now_epoch=now)

        # --- durable per-update handling (begin -> handler -> complete) ------
        handled = 0
        skipped_done = 0
        handler_failures = 0
        for update in sorted(updates, key=lambda item: item.update_id):
            update_id = update.update_id
            status = self._inbox.begin(update_id, now_epoch=now)
            if status is InboxStatus.DONE:
                skipped_done += 1
                continue
            try:
                await self._handler.handle(update)
            except Exception as exc:
                # leave the row pending: the pinned offset re-delivers it and
                # the idempotent handler retries the effects. One bad update
                # never stops the loop and is never marked done.
                handler_failures += 1
                logger.warning(
                    "update %d handler failed: %s", update_id, type(exc).__name__
                )
                continue
            # the clock is read AGAIN: awaits above consumed real time and a
            # stale value here must never leak into durable effects
            self._inbox.complete(update_id, now_epoch=self._clock())
            handled += 1

        # --- tick only after a successful, health-confirmed poll -------------
        ticked = False
        tick_failed = False
        if not self._unsafe:
            try:
                report = await self._runtime.tick(
                    now_epoch=self._clock()  # fresh: every await above moved time
                )
            except Exception as exc:
                tick_failed = True
                self._unsafe = True
                logger.warning("runtime tick failed: %s", type(exc).__name__)
            else:
                ticked = True
                for result in report.sent:
                    if (
                        result.outcome == "uncertain"
                        and result.code in _UNSAFE_SEND_CODES
                    ):
                        self._unsafe = True
        return CycleReport(
            ok=True,
            fetched=len(updates),
            handled=handled,
            skipped_done=skipped_done,
            handler_failures=handler_failures,
            pending_after=tuple(self._inbox.pending_ids()),
            reconnect=reconnect,
            outage_start=outage_start,
            downtime_seconds=downtime_seconds,
            parked_uncertain=parked_uncertain,
            parked_missed=parked_missed,
            notice_queued=notice_queued,
            ticked=ticked,
            tick_failed=tick_failed,
            unsafe=self._unsafe,
            sleep_seconds=0.0,
        )

    def _reconnect_detected(
        self, prev_poll: int | None, now: int, *, ended_failure_streak: bool
    ) -> bool:
        """A poll success counts as a reconnect when it ends ANY run of
        consecutive failures or lands beyond the outage tolerance from the
        last successful poll."""
        if ended_failure_streak:
            return True
        return prev_poll is not None and now - prev_poll > OUTAGE_TOLERANCE_SECONDS

    def _failure_report(
        self, code: str, class_name: str, *, category: str = "unknown",
        duration_ms: int | None = None, retry_after: float | None = None,
    ) -> CycleReport:
        """Failed poll: no heartbeat, no sweep, no updates, no tick — and
        therefore no channel sends. Backoff for the runner to wait on."""
        delay = _backoff_seconds(
            self._consecutive_failures, transient=code in {"timeout", "transport_error"}
        )
        if retry_after is not None:
            delay = max(delay, retry_after)
        logger.warning(
            "poll failed: code=%s class=%s category=%s duration_ms=%s retry_seconds=%s",
            code, class_name, category, duration_ms, delay,
        )
        return CycleReport(
            ok=False,
            failure_code=code,
            failure_class=class_name,
            failure_category=category,
            request_duration_ms=duration_ms,
            pending_after=tuple(self._inbox.pending_ids()),
            unsafe=self._unsafe,
            sleep_seconds=delay,
        )
