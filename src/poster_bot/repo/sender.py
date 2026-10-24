"""Sender: turn due publications into durable outcomes via the transport.

Ordering rule (PLAN §5): the claim and its pre-call attempt row are persisted
BEFORE any channel call, so a crash at any point leaves durable state for the
recovery sweep (a sending publication with an unfinished attempt becomes
'uncertain'). One outcome per claimed publication: sent -> published,
definitive failure -> missed (never silently rescheduled), uncertain ->
uncertain. No retries, ever.

Gated planned path (CHUNK4): when composed WITH plans/receipts/editor (all
three or the wiring is refused) sending is plan-driven and fail-closed:

- a due publication whose post has no frozen plan is never published
  unsigned. With a configured signature a legacy planless COPY is first
  AUTO-PREPARED before the claim: its stored content is folded losslessly
  (the identical pure rule the preview uses), frozen by the deterministic
  planner under that signature and persisted as one atomic pair-only write —
  the posts row, ids, schedule and provenance stay byte-identical, and the
  embedded signature survives any later ambient config change. Preparation
  runs only inside the healthy due window (never as late catch-up), touches
  no channel and rolls back to nothing on refusal;
- what cannot be prepared (a forward, content the planner blocks even with
  the mandatory footer, e.g. an un-splittable oversized paragraph) is SKIPPED
  BEFORE the claim (no attempt row, schedule stays intact): the owner is
  prompted once per process — publication id and machine codes only, never
  raw content — to re-compose through the preview. An offline, read-only
  migration readiness scan (runtime.plan_migration_readiness) surfaces the
  same ids and codes before a cutover;
- the send source is the durable intent rows the claim snapshotted inside its
  own transaction — the frozen plan, never posts.content_json and never an
  ambient re-plan of the current signature config;
- every ack is recorded as a durable part receipt BEFORE the next part is
  sent; parts run strictly in plan order and the stack stops at the first
  failure: with zero confirmed parts a definitive failure parks 'missed',
  anything else (uncertain, or any failure after a confirmation) parks
  'uncertain' with the failed part recorded and the not-attempted parts
  failed — a pending row beside a resolved attempt stays reserved as durable
  "outcome unknown" evidence for a hard crash only;
- after the publication completed as published, a navigation pass edits each
  received part exactly once (links over the frozen footer slots, URLs from
  sibling receipts only). A failed or crashed edit never re-sends a body,
  never changes the published status, never propagates and is never retried;
  it ends with one owner notice about the incomplete navigation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..domain.content import Entity
from ..domain.errors import (
    ContentValidationError,
    PublicationNotFound,
    TransitionError,
)
from ..domain.normalization import prepare_legacy
from ..domain.original_forward import OriginalForwardPlan, plan_primary_content
from ..domain.planner import PlannedPart
from ..domain.signature import SignatureConfig
from ..repo.dispatch import DispatchService
from ..repo.plans import PlanStore
from ..repo.receipts import PartReceiptStore, PublicationPart
from ..transport.ports import (
    ChannelTransport,
    DefinitiveFailure,
    NavEditRequest,
    PublishOutcome,
    Sent,
    TransportCallError,
    UncertainOutcome,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SendResult:
    """Summary of one claimed publication's outcome for callers/UI."""

    publication_id: int
    outcome: str  # published | missed | uncertain
    code: str | None = None
    message_ids: tuple[int, ...] = ()


def _base_entities(part: PlannedPart) -> tuple[Entity, ...]:
    """The entity tuple the transport originally carried for this part."""
    content = part.content
    if content.kind == "text":
        return content.entities
    if content.kind == "album":
        return content.caption_entities
    if content.media is not None:
        return content.media.caption_entities
    return ()


class SenderService:
    def __init__(
        self,
        dispatch: DispatchService,
        transport: ChannelTransport,
        *,
        plans: PlanStore | None = None,
        receipts: PartReceiptStore | None = None,
        editor=None,
        signature: SignatureConfig | None = None,
        owner_user_id: int | None = None,
        channel_id: int | None = None,
    ) -> None:
        provided = (plans is not None, receipts is not None, editor is not None)
        if any(provided) and not all(provided):
            raise ValueError(
                "planned sending needs plans, receipts and editor together")
        if signature is not None and plans is None:
            raise ValueError(
                "a configured signature is only meaningful on the gated"
                " planned path")
        self._dispatch = dispatch
        self._transport = transport
        self._plans = plans
        self._receipts = receipts
        self._editor = editor
        # Trusted composition context, never inferred from a persisted plan.
        # Absent defaults preserve signed callers but forbid native forwarding.
        self._owner_user_id = owner_user_id
        self._channel_id = channel_id
        # configured signature used ONLY to auto-prepare a legacy planless
        # copy (see prepare_plan); once a pair is persisted its EMBEDDED
        # signature governs -- a later ambient config change never replans
        self._signature = signature if plans is not None else None
        # in-process only: planless publications the owner was already
        # prompted about this process (the durable fact is the missing plan,
        # the prompt must not repeat on every tick)
        self._planless_prompted: set[int] = set()
        # in-process: machine codes (never raw content) of why the last
        # preparation for a planless publication was refused
        self._planless_codes: dict[int, tuple[str, ...]] = {}

    async def send_due(self, *, now_epoch: int) -> list[SendResult]:
        """Claim and publish everything inside its healthy due window now.

        Publications outside their window are never touched here: 'too_early'
        waits for the clock, 'past_due' belongs to the recovery sweep
        ('missed'), which keeps late replays from ever sending.
        """
        results: list[SendResult] = []
        for pub in self._dispatch.due_publications(now_epoch=now_epoch):
            if self._plans is not None and not self._plans.has_plan(pub.post_id):
                # legacy planless copy: with a configured signature the pair
                # is auto-prepared and frozen BEFORE the claim (approved
                # mandatory-signature upgrade); without a signature — or when
                # preparation is refused — the fail-closed refusal stands
                if self._signature is None or not self.prepare_plan(
                        pub, now_epoch=now_epoch):
                    await self._refuse_planless(pub)
                    continue
            if self._plans is not None:
                stored = self._plans.get(pub.post_id)
                if stored is not None and isinstance(stored.plan, OriginalForwardPlan):
                    try:
                        stored.plan.validate_binding(owner_user_id=self._owner_user_id,
                                                     channel_id=self._channel_id)
                    except ContentValidationError:
                        if pub.id not in self._planless_prompted:
                            self._planless_prompted.add(pub.id)
                            await self._notify(
                                f"Публикация {pub.id} не отправлена: привязка"
                                " плана пересылки к владельцу или каналу не"
                                " подтверждена. Проверьте план вручную.")
                        continue
            try:
                attempt_id = self._dispatch.claim(
                    pub.id, pub.version, now_epoch=now_epoch,
                    owner_user_id=self._owner_user_id if self._plans is not None else None,
                    channel_id=self._channel_id if self._plans is not None else None,
                )
            except TransitionError:
                # lost the race or the window moved on; already journaled
                continue
            self._dispatch.mark_in_progress(attempt_id, now_epoch=now_epoch)
            if self._receipts is None:
                results.append(
                    await self._send_legacy(pub, attempt_id, now_epoch))
            else:
                results.append(
                    await self._send_planned(pub, attempt_id, now_epoch))
        return results

    # -- legacy single-message path (unchanged behavior) ----------------------

    async def _send_legacy(self, pub, attempt_id: int,
                           now_epoch: int) -> SendResult:
        content = self._dispatch.content_for_publication(pub.id)
        try:
            outcome = await self._transport.publish(content)
        except Exception as exc:
            # an adapter-level crash must still resolve the publication
            # durably before the error is surfaced.
            self._dispatch.mark_uncertain(
                pub.id, attempt_id, "transport_error", now_epoch=now_epoch
            )
            # log hygiene: exception text and tracebacks can echo post
            # content or request fragments -- class name only, never
            # str(exc) and no exc_info/traceback (PLAN: no payloads).
            logger.error(
                "transport crashed on publication %d: class=%s",
                pub.id,
                type(exc).__name__,
            )
            raise
        return self._record(pub, attempt_id, outcome, now_epoch)

    def _record(
        self,
        pub,
        attempt_id: int,
        outcome: PublishOutcome,
        now_epoch: int,
    ) -> SendResult:
        match outcome:
            case Sent(message_ids=message_ids, permalink=permalink):
                self._dispatch.complete(
                    pub.id, attempt_id, message_ids, permalink, now_epoch=now_epoch
                )
                return SendResult(
                    pub.id, "published", message_ids=message_ids
                )
            case DefinitiveFailure(code=code):
                self._dispatch.fail_definitive(
                    pub.id, attempt_id, code, now_epoch=now_epoch
                )
                return SendResult(pub.id, "missed", code=code)
            case UncertainOutcome(code=code):
                self._dispatch.mark_uncertain(
                    pub.id, attempt_id, code, now_epoch=now_epoch
                )
                return SendResult(pub.id, "uncertain", code=code)
            case unknown:  # a transport violating the outcome contract
                raise TransitionError(f"unknown send outcome {unknown!r}")

    # -- gated planned path ----------------------------------------------------

    def prepare_plan(self, pub, *, now_epoch: int) -> bool:
        """Auto-prepare one legacy planless COPY publication (CHUNK4 fix).

        Runs at most once per publication, only inside its healthy due window
        (the due loop is the only caller), and only when plans-gated with a
        configured signature: the stored posts.content_json is folded through
        the SAME pure lossless normalizer the preview uses, frozen by the
        deterministic planner under the CONFIGURED signature and persisted as
        one atomic pair-only write (posts row, ids, settings, schedule,
        version and provenance untouched). From that instant the publication
        behaves like any planned one: preview and claim read the SAME stored
        pair, and every later send uses the EMBEDDED signature.

        Nothing channel-facing happens here. On refusal (forward, unparseable
        content, planner-blocked pair, save rejected) the machine codes are
        recorded for the owner notice, NOTHING is written, the schedule stays
        intact and the fail-closed refusal keeps the publication away from
        the channel until the owner re-composes through the preview."""
        try:
            content = self._dispatch.content_for_publication(pub.id)
        except (PublicationNotFound, ContentValidationError):
            self._planless_codes[pub.id] = ("content_invalid",)
            return False
        preparation = prepare_legacy(content, self._signature)
        if not preparation.preparable:
            self._planless_codes[pub.id] = preparation.codes
            return False
        try:
            self._plans.save(pub.post_id, preparation.logical,
                             preparation.plan, now_epoch=now_epoch)
        except (ContentValidationError, TransitionError, PublicationNotFound):
            # the pair the planner just produced must pass its own save gate;
            # a frozen/racing status is refused loudly, never bypassed
            self._planless_codes[pub.id] = ("plan_rejected",)
            return False
        logger.info(
            "auto-prepared frozen plan for publication %d (post %d): %d part(s)",
            pub.id, pub.post_id, len(preparation.plan.parts))
        return True

    async def _refuse_planless(self, pub) -> None:
        """Fail-closed refusal: never publish unsigned, keep the schedule,
        prompt the owner once per process to re-compose via the preview.
        Carries the publication id and, when preparation was attempted and
        refused, its machine codes — never raw user content."""
        if pub.id in self._planless_prompted:
            return
        self._planless_prompted.add(pub.id)
        codes = self._planless_codes.get(pub.id)
        code_text = f" Код: {', '.join(codes)}." if codes else ""
        await self._notify(
            f"Публикация {pub.id} не отправлена: план публикации не заморожен."
            f"{code_text} Откройте предпросмотр и сохраните план, чтобы"
            " разрешить отправку."
        )

    async def _send_planned(self, pub, attempt_id: int,
                            now_epoch: int) -> SendResult:
        parts = self._receipts.parts_of_attempt(attempt_id)
        if not parts or any(row.snapshot is None for row in parts):
            # defense in depth: the claim snapshots a complete intent per
            # part in its own transaction; a missing snapshot means durable
            # state was broken outside the approved write path
            raise TransitionError(
                f"planned attempt {attempt_id} has no complete intent snapshot")
        permalinks: dict[int, str] = {}
        union: list[int] = []
        received: list[PublicationPart] = []  # rows WITH receipts, nav targets
        native = any(isinstance(row.snapshot, OriginalForwardPlan) for row in parts)
        if native and (len(parts) != 1 or parts[0].part_index != 1 or parts[0].part_count != 1):
            raise TransitionError("native forward must be one complete operation")
        for row in parts:
            snapshot = row.snapshot
            if isinstance(snapshot, OriginalForwardPlan):
                snapshot.validate_binding(owner_user_id=self._owner_user_id,
                                          channel_id=self._channel_id)
                content = plan_primary_content(snapshot)
            elif snapshot.published_text() is None or snapshot.content.kind == "forward":
                # unreachable for plans the planner produced (every planned
                # part carries the mandatory signature and no forward): a
                # forged snapshot that survived claim re-parsing is refused
                # loudly before any send
                raise TransitionError(
                    f"planned part {row.part_index} is not sendable")
            else:
                content = snapshot.content
            try:
                outcome = await self._transport.publish(content)
            except Exception as exc:
                # adapter crash: this part's outcome is unknowable, the rest
                # was never attempted; resolve durably, then surface (legacy
                # parity: class-name-only log, exception re-raised)
                self._receipts.record_uncertain(attempt_id, row.part_index,
                                                now_epoch=now_epoch)
                self._fail_remaining(parts, attempt_id, row.part_index,
                                     now_epoch)
                self._dispatch.mark_uncertain(pub.id, attempt_id,
                                              "transport_error",
                                              now_epoch=now_epoch)
                await self._notify(
                    f"Публикация {pub.id}: отправка остановлена, исход одной"
                    " из частей неизвестен. Проверьте канал вручную;"
                    " автоматический повтор запрещён."
                )
                logger.error(
                    "transport crashed on publication %d: class=%s",
                    pub.id, type(exc).__name__,
                )
                raise
            match outcome:
                case Sent(message_ids=message_ids, permalink=permalink):
                    if native and (
                        not isinstance(message_ids, (tuple, list))
                        or len(message_ids) != len(snapshot.source.message_ids)
                        or any(type(mid) is not int or mid <= 0 for mid in message_ids)
                        or len(set(message_ids)) != len(message_ids)
                    ):
                        return await self._stop_uncertain(
                            pub, attempt_id, parts, row.part_index,
                            "partial_result", now_epoch)
                    stored = self._receipts.record_confirmed(
                        attempt_id, row.part_index, message_ids, permalink,
                        now_epoch=now_epoch)
                    permalinks[row.part_index] = stored.permalink
                    union.extend(stored.channel_message_ids)
                    received.append(stored)
                case DefinitiveFailure(code=code) if permalinks:
                    # something already landed: even a definitive rejection
                    # of this part leaves the channel state doubtful
                    return await self._stop_uncertain(
                        pub, attempt_id, parts, row.part_index, code,
                        now_epoch)
                case DefinitiveFailure(code=code):
                    # nothing ever landed: the whole stack missed
                    for pending in parts:
                        self._receipts.record_failed(attempt_id,
                                                     pending.part_index,
                                                     now_epoch=now_epoch)
                    self._dispatch.fail_definitive(pub.id, attempt_id, code,
                                                   now_epoch=now_epoch)
                    if native:
                        await self._notify(
                            f"Публикация {pub.id} не отправлена: пересылка"
                            " отклонена. Проверьте источник и права;"
                            " автоматический повтор запрещён.")
                    return SendResult(pub.id, "missed", code=code)
                case UncertainOutcome(code=code):
                    return await self._stop_uncertain(
                        pub, attempt_id, parts, row.part_index, code,
                        now_epoch)
                case unknown:  # a transport violating the outcome contract
                    raise TransitionError(f"unknown send outcome {unknown!r}")
        # every part confirmed: complete first (history provenance is the
        # receipts), then the links-only navigation pass
        self._dispatch.complete(pub.id, attempt_id, tuple(sorted(union)),
                                permalinks[parts[0].part_index],
                                now_epoch=now_epoch)
        if not native:
            await self._navigation_pass(pub, attempt_id, received, permalinks,
                                        now_epoch)
        return SendResult(pub.id, "published",
                          message_ids=tuple(sorted(union)))

    async def _stop_uncertain(self, pub, attempt_id: int,
                              parts: list[PublicationPart], failed_index: int,
                              code: str, now_epoch: int) -> SendResult:
        self._receipts.record_uncertain(attempt_id, failed_index,
                                        now_epoch=now_epoch)
        self._fail_remaining(parts, attempt_id, failed_index, now_epoch)
        self._dispatch.mark_uncertain(pub.id, attempt_id, code,
                                      now_epoch=now_epoch)
        await self._notify(
            f"Публикация {pub.id}: отправка остановлена, исход одной из частей"
            " неизвестен. Проверьте канал вручную; автоматический повтор"
            " запрещён."
        )
        return SendResult(pub.id, "uncertain", code=code)

    def _fail_remaining(self, parts: list[PublicationPart], attempt_id: int,
                        failed_index: int, now_epoch: int) -> None:
        # not-attempted parts get a durable TERMINAL outcome: pending rows
        # beside a resolved attempt stay reserved as crash evidence only
        for row in parts:
            if row.part_index > failed_index:
                self._receipts.record_failed(attempt_id, row.part_index,
                                             now_epoch=now_epoch)

    # -- navigation pass ---------------------------------------------------------

    async def _navigation_pass(self, pub, attempt_id: int,
                               parts: list[PublicationPart],
                               permalinks: dict[int, str],
                               now_epoch: int) -> None:
        """Edit confirmed parts exactly once to attach the frozen footer's
        nav links (URLs from sibling receipts only). Bodies are immutable:
        text is byte-identical to the sent part. Failures never propagate,
        never re-send and are never retried."""
        broken = False
        for row in parts:
            snapshot = row.snapshot
            footer = snapshot.footer
            if footer is None or not any(s.linked for s in footer.slots):
                continue
            request = NavEditRequest(
                message_id=row.channel_message_ids[0],
                text=snapshot.published_text(),
                entities=self._nav_entities(snapshot, permalinks),
                kind="text" if snapshot.content.kind == "text" else "caption",
            )
            self._receipts.begin_nav_edit(attempt_id, row.part_index,
                                          now_epoch=now_epoch)
            try:
                outcome = await self._editor.edit_published_part(request)
            except Exception as exc:
                # editor crash: recorded with the safe generic code; the pass
                # continues over the remaining parts and never propagates
                self._receipts.fail_nav_edit(attempt_id, row.part_index,
                                             "transport_error",
                                             now_epoch=now_epoch)
                logger.error(
                    "navigation edit crashed on publication %d part %d:"
                    " class=%s", pub.id, row.part_index, type(exc).__name__,
                )
                broken = True
                continue
            match outcome:
                case Sent():
                    self._receipts.confirm_nav_edit(attempt_id,
                                                    row.part_index,
                                                    now_epoch=now_epoch)
                case DefinitiveFailure(code=code) | UncertainOutcome(code=code):
                    self._receipts.fail_nav_edit(attempt_id, row.part_index,
                                                 code, now_epoch=now_epoch)
                    broken = True
                case unknown:  # an editor violating the outcome contract
                    self._receipts.fail_nav_edit(attempt_id, row.part_index,
                                                 "transport_error",
                                                 now_epoch=now_epoch)
                    logger.error(
                        "navigation editor returned invalid outcome on"
                        " publication %d part %d", pub.id, row.part_index)
                    broken = True
        if broken:
            await self._notify(
                f"Публикация {pub.id}: навигация применена не полностью."
                " Проверьте ссылки в канале вручную; текст публикации не"
                " менялся."
            )

    @staticmethod
    def _nav_entities(part: PlannedPart, permalinks: dict[int, str]
                      ) -> tuple[Entity, ...]:
        """Original entities plus 'text_link' entities at the LINKED slot
        offsets (plan slot order); the part's own label stays plain."""
        links = tuple(
            Entity(type="text_link", offset=slot.utf16_offset,
                   length=slot.utf16_length, url=permalinks[slot.part_index])
            for slot in part.footer.slots if slot.linked
        )
        return _base_entities(part) + links

    # -- owner notice (best effort, never breaks the send path) ------------------

    async def _notify(self, text: str) -> None:
        try:
            await self._transport.notify_owner(text)
        except TransportCallError as exc:
            logger.warning("sender owner notice failed: code=%s", exc.code)
