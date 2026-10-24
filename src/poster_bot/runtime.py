"""Runtime: the tick logic the poll loop drives with a real clock.

Deliberately clock-injectable and transport-injected so the whole recovery
story can be tested offline with a fake clock and fake transport:

- each tick advances the liveness heartbeat, then sends only what is inside
  its healthy due window (window enforcement lives in the claim itself, so a
  stale or replayed tick can never re-send anything);
- network outages are survived IN-PROCESS by the poll loop (UI stage, wired):
  a failed poll freezes the heartbeat and skips the tick, so there are no
  channel sends while connectivity is unproven; the next successful poll is
  treated as a reconnect, runs the recovery sweep and persists one downtime
  notice. A process death is detected on restart the same way, from the
  stored heartbeat (open_for_run);
- notice delivery is at-least-once over the outbox: a failed channel call
  leaves the notice pending for the next tick, a successful one marks it
  sent. At-least-once means a crash strictly between a delivered API call
  and its mark_sent recording can redeliver the same notice after restart;
  that duplicate is accepted (losing a notice is worse than repeating one),
  so no exactly-once guarantee is made anywhere here;
- daily maintenance (journal retention, inbox compaction) runs at the END
  of a tick, best-effort and never before sends, so maintenance can never
  add send-safety risk; every failure is logged and skipped. It uses the
  injected runtime clock only.

The notice text itself is chunked below Telegram's message limit: one
logical notice can span several messages and is marked sent only after the
last part was delivered (re-sending already-delivered parts after a partial
failure follows from the same at-least-once rule).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from collections.abc import Awaitable, Callable

from .db import iso
from .domain.content import Content
from .domain.errors import ContentValidationError
from .domain.normalization import PREPARABLE, prepare_legacy
from .domain.original_forward import OriginalForwardPlan
from .domain.signature import SignatureConfig
from .journal import Journal
from .repo.dispatch import DispatchService
from .repo.inbox import InboxRepo
from .repo.notifications import Notice, OwnerNoticeStore
from .repo.plans import PlanStore
from .repo.receipts import PartReceiptStore
from .repo.runtime_state import RuntimeState
from .repo.sender import SendResult, SenderService
from .transport.ports import ChannelTransport, TransportCallError

logger = logging.getLogger(__name__)

#: maintenance cadence measured in injected runtime time: the first tick of
#: a process runs it, then once per interval (journal retention, inbox
#: compaction). Restart re-arms it; prune/compact are idempotent anyway.
MAINTENANCE_INTERVAL_SECONDS = 24 * 3600

#: Telegram hard-caps one message at 4096 characters; parts stay below that
#: with margin for the continuation prefix.
NOTICE_PART_MAX_CHARS = 3_500


def notice_parts(notice: Notice) -> tuple[str, ...]:
    """Owner-facing downtime notice in Russian: interval plus affected
    publication ids, split into message-sized parts when the id list is long.

    Times are ISO-8601 UTC (the ``+00:00`` marker is explicit in every
    stamp). Only ids and epochs — no secrets, no payload content.
    """
    header = (
        "poster-bot: простой с "
        f"{iso(notice.downtime_start_epoch)} по "
        f"{iso(notice.downtime_end_epoch)} (UTC). "
    )
    if not notice.affected_ids:
        return (header + "Затронутых публикаций нет.",)
    prefix = header + "Затронуты публикации: "
    continuation = "Затронуты публикации (продолжение): "
    parts: list[str] = []
    current = prefix
    has_ids = False
    for sid in (str(i) for i in notice.affected_ids):
        candidate = current + sid if not has_ids else current + ", " + sid
        if len(candidate) > NOTICE_PART_MAX_CHARS and has_ids:
            parts.append(current)
            current = continuation + sid
            has_ids = True
            continue
        # the first id always fits: the header is a fraction of the budget
        current = candidate
        has_ids = True
    parts.append(current)
    return tuple(parts)


def notice_text(notice: Notice) -> str:
    """Full notice text (all parts joined). Delivery uses ``notice_parts``."""
    return "\n".join(notice_parts(notice))


@dataclass(frozen=True)
class TickReport:
    sent: tuple[SendResult, ...] = ()
    notices_delivered: tuple[int, ...] = ()
    notices_pending: tuple[int, ...] = ()
    #: housekeeping removed counts from this tick's maintenance step
    #: (not part of ``anything_happened``: sends and notices only)
    journal_pruned: int = 0
    inbox_compacted: int = 0

    @property
    def anything_happened(self) -> bool:
        return bool(self.sent or self.notices_delivered or self.notices_pending)


class Runtime:
    """Composition of sender + notice delivery + heartbeat around one conn.

    The transport is injected: live composition passes the aiogram adapter,
    tests pass the fake. Nothing here imports aiogram. The journal and the
    durable inbox are injected too (``for_app`` wires both from the opened
    App); when absent the maintenance step simply does nothing, so older
    construction sites keep working unchanged.
    """

    def __init__(
        self,
        *,
        dispatch: DispatchService,
        notices: OwnerNoticeStore,
        runtime_state: RuntimeState,
        transport: ChannelTransport,
        journal: Journal | None = None,
        inbox: InboxRepo | None = None,
        refresh_dashboard: Callable[[int], Awaitable[None]] | None = None,
        plans: PlanStore | None = None,
        receipts: PartReceiptStore | None = None,
        signature: SignatureConfig | None = None,
        owner_user_id: int | None = None,
        channel_id: int | None = None,
    ) -> None:
        self._dispatch = dispatch
        self._notices = notices
        self._runtime = runtime_state
        # gated live composition injects plans/receipts: the sender then runs
        # the plan-driven path with the transport itself as the channel
        # editor; without them the legacy path stays untouched. The injected
        # signature is used by the sender ONLY to auto-prepare a legacy
        # planless copy before its claim; persisted pairs keep the signature
        # they embed.
        self._sender = SenderService(
            dispatch, transport, plans=plans, receipts=receipts,
            editor=transport if plans is not None else None,
            signature=signature,
            owner_user_id=owner_user_id, channel_id=channel_id,
        )
        self._transport = transport
        self._journal = journal
        self._inbox = inbox
        self._refresh_dashboard = refresh_dashboard
        # in-process maintenance throttle, measured in injected runtime
        # time; durable facts stay in the DB (this only paces attempts)
        self._next_maintenance_at: int | None = None

    @classmethod
    def for_app(cls, app, transport: ChannelTransport, *,
                refresh_dashboard: Callable[[int], Awaitable[None]] | None = None,
                plans: PlanStore | None = None,
                receipts: PartReceiptStore | None = None,
                signature: SignatureConfig | None = None,
                owner_user_id: int | None = None,
                channel_id: int | None = None) -> "Runtime":
        """Compose the runtime around an opened App (app.py stays aiogram-free;
        the live caller injects the adapter). Passing ``plans`` and
        ``receipts`` activates the gated planned send path; ``signature``
        additionally enables the due-time auto-prepare of legacy planless
        copies on that path."""
        return cls(
            dispatch=app.dispatch,
            notices=app.notices,
            runtime_state=app.runtime,
            transport=transport,
            journal=app.journal,
            inbox=InboxRepo(app.conn),
            refresh_dashboard=refresh_dashboard,
            plans=plans,
            receipts=receipts,
            signature=signature,
            owner_user_id=owner_user_id, channel_id=channel_id,
        )

    # -- polling support hooks (used by the UI stage) ----------------------

    def record_poll_success(self, *, now_epoch: int | None = None) -> None:
        self._runtime.set_poll(now_epoch=now_epoch)

    def heartbeat(self, *, now_epoch: int | None = None) -> None:
        self._runtime.set_alive(now_epoch=now_epoch)

    # -- ticks -------------------------------------------------------------

    async def tick(self, *, now_epoch: int) -> TickReport:
        """One runtime cycle at an injected clock instant."""
        self._runtime.set_alive(now_epoch=now_epoch)
        results = await self._sender.send_due(now_epoch=now_epoch)
        # Sender outcomes are already committed. UI retries are independently
        # paced by the UI hook; they can never cause another channel publish.
        # Invoked on healthy ticks only, including ticks with pending UI work
        # but no sends, and checks its frame fingerprint before any API edit.
        if self._refresh_dashboard is not None:
            try:
                await self._refresh_dashboard(now_epoch)
            except Exception as exc:
                logger.warning("dashboard hook failed: class=%s", type(exc).__name__)
        delivered, pending = await self.deliver_notices(now_epoch=now_epoch)
        # maintenance AFTER all sends: housekeeping must never delay or
        # endanger the due-window pipeline, and its failure changes nothing
        pruned, compacted = self._run_maintenance(now_epoch=now_epoch)
        return TickReport(tuple(results), delivered, pending, pruned, compacted)

    def _run_maintenance(self, *, now_epoch: int) -> tuple[int, int]:
        """Best-effort daily housekeeping at the injected runtime clock:

        - journal retention: drop events past ``RETENTION_DAYS`` (only
          ``journal_events`` rows -- drafts and publication history are
          separate tables and are never touched here);
        - inbox compaction: drop done receipts below the durable resume
          floor (see ``InboxRepo.compact_completed_below_floor``).

        The throttle paces ATTEMPTS in runtime time: an attempt at an
        instant advances the schedule whether or not every step succeeded,
        so a permanently broken step can never hammer the loop every cycle.
        A clock that rewinds simply delays the next attempt (monotonic
        guard). Re-armed by restart; both steps are idempotent."""
        if self._journal is None and self._inbox is None:
            return 0, 0
        due_at = self._next_maintenance_at
        if due_at is not None and now_epoch < due_at:
            return 0, 0
        self._next_maintenance_at = now_epoch + MAINTENANCE_INTERVAL_SECONDS
        pruned = compacted = 0
        if self._journal is not None:
            try:
                pruned = self._journal.prune(now_epoch=now_epoch)
                if pruned:
                    self._journal.record(
                        "journal_pruned",
                        now_epoch=now_epoch,
                        removed_count=pruned,
                    )
            except Exception as exc:
                pruned = 0
                logger.warning(
                    "journal prune failed: %s (next attempt in %ds)",
                    type(exc).__name__, MAINTENANCE_INTERVAL_SECONDS,
                )
        if self._inbox is not None:
            try:
                compacted = self._inbox.compact_completed_below_floor()
            except Exception as exc:
                compacted = 0
                logger.warning(
                    "inbox compaction failed: %s (next attempt in %ds)",
                    type(exc).__name__, MAINTENANCE_INTERVAL_SECONDS,
                )
        return pruned, compacted

    async def deliver_notices(self, *, now_epoch: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """Drain the pending outbox once; failures stay pending (never lost).

        One notice may span several message parts (long affected-id lists);
        it is marked sent only after the LAST part is delivered. A failure at
        any part keeps the whole notice pending, so the next tick re-sends
        every part -- accepted duplication of the at-least-once contract."""
        delivered: list[int] = []
        pending: list[int] = []
        for notice in self._notices.pending():
            try:
                for part in notice_parts(notice):
                    await self._transport.notify_owner(part)
            except TransportCallError as exc:
                logger.warning(
                    "notice %d delivery failed: %s (stays pending)", notice.id, exc.code
                )
                pending.append(notice.id)
                continue
            self._notices.mark_sent(notice.id, now_epoch=now_epoch)
            delivered.append(notice.id)
        return tuple(delivered), tuple(pending)


# ---------------------------------------------------------------------------
# offline migration readiness preflight (cutover operator check)
# ---------------------------------------------------------------------------

#: readiness codes owned by this scan (the content-level codes — 'preparable',
#: 'forward', 'empty_post', 'content_invalid' and the planner's own — come
#: from domain.normalization.prepare_legacy unchanged)
READINESS_PLANNED = "planned"
READINESS_PLAN_CORRUPT = "plan_corrupt"
READINESS_MISSING_CONTENT = "missing_content"


@dataclass(frozen=True)
class ReadinessItem:
    """One still-publishable publication: id, post, status and machine codes
    ONLY — readiness reports never carry raw user content."""

    publication_id: int
    post_id: int
    status: str
    codes: tuple[str, ...]

    @property
    def preparable(self) -> bool:
        """True when sending needs no operator action before cutover: the
        post already has a frozen plan, or the gated sender's due-time
        auto-prepare will freeze an identical one without touching the post."""
        return self.codes in ((READINESS_PLANNED,), (PREPARABLE,))


@dataclass(frozen=True)
class MigrationReadiness:
    items: tuple[ReadinessItem, ...]

    @property
    def blockers(self) -> tuple[ReadinessItem, ...]:
        return tuple(item for item in self.items if not item.preparable)

    @property
    def ready_for_cutover(self) -> bool:
        """Every scheduled/suspended publication has a locally accepted send
        path (signed copy or explicitly bound native forward)."""
        return not self.blockers


def plan_migration_readiness(conn, signature: SignatureConfig, *,
                             owner_user_id: int | None = None,
                             channel_id: int | None = None,
                             ) -> MigrationReadiness:
    """READ-ONLY pre-cutover scan: for every scheduled/suspended publication
    report whether sending is safe under the signature this deployment will
    publish under ('preparable'/'planned') or why it needs owner review
    ('forward', 'manual_split_required', 'plan_corrupt', ...).

    Offline contract: pure SELECTs plus the deterministic planner — no writes,
    no clock, no environment access, no network. The caller passes the
    signature it intends to deploy (the function never reads env itself).
    Legacy raw forwards remain blockers: only an explicit native input/plan
    pair with independent trusted owner/resolved-channel binding is accepted.
    This checks local snapshots, never remote source availability."""
    if not isinstance(signature, SignatureConfig):
        raise ValueError("plan_migration_readiness needs the SignatureConfig "
                         "this deployment will publish under")
    plans = PlanStore(conn)
    rows = conn.execute(
        "SELECT id, post_id, status FROM publications"
        " WHERE status IN ('scheduled','suspended') ORDER BY id").fetchall()
    items: list[ReadinessItem] = []
    for row in rows:
        try:
            stored = plans.get(row["post_id"])
            if stored is not None and isinstance(stored.plan, OriginalForwardPlan):
                stored.plan.validate_binding(owner_user_id=owner_user_id,
                                             channel_id=channel_id)
        except ContentValidationError:
            items.append(ReadinessItem(row["id"], row["post_id"],
                                       row["status"],
                                       (READINESS_PLAN_CORRUPT,)))
            continue
        if stored is not None:
            items.append(ReadinessItem(row["id"], row["post_id"],
                                       row["status"], (READINESS_PLANNED,)))
            continue
        content_row = conn.execute(
            "SELECT content_json FROM posts WHERE id=?",
            (row["post_id"],)).fetchone()
        if content_row is None:
            items.append(ReadinessItem(row["id"], row["post_id"],
                                       row["status"],
                                       (READINESS_MISSING_CONTENT,)))
            continue
        try:
            content = Content.from_json(content_row["content_json"])
        except ContentValidationError:
            items.append(ReadinessItem(row["id"], row["post_id"],
                                       row["status"], ("content_invalid",)))
            continue
        items.append(ReadinessItem(row["id"], row["post_id"], row["status"],
                                   prepare_legacy(content, signature).codes))
    return MigrationReadiness(tuple(items))
