"""Sender: turn due publications into durable outcomes via the transport.

Ordering rule (PLAN §5): the claim and its pre-call attempt row are persisted
BEFORE any channel call, so a crash at any point leaves durable state for the
recovery sweep (a sending publication with an unfinished attempt becomes
'uncertain'). One outcome per claimed publication: sent -> published,
definitive failure -> missed (never silently rescheduled), uncertain ->
uncertain. No retries, ever.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..domain.errors import TransitionError
from ..repo.dispatch import DispatchService
from ..transport.ports import (
    ChannelTransport,
    DefinitiveFailure,
    PublishOutcome,
    Sent,
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


class SenderService:
    def __init__(self, dispatch: DispatchService, transport: ChannelTransport) -> None:
        self._dispatch = dispatch
        self._transport = transport

    async def send_due(self, *, now_epoch: int) -> list[SendResult]:
        """Claim and publish everything inside its healthy due window now.

        Publications outside their window are never touched here: 'too_early'
        waits for the clock, 'past_due' belongs to the recovery sweep
        ('missed'), which keeps late replays from ever sending.
        """
        results: list[SendResult] = []
        for pub in self._dispatch.due_publications(now_epoch=now_epoch):
            try:
                attempt_id = self._dispatch.claim(
                    pub.id, pub.version, now_epoch=now_epoch
                )
            except TransitionError:
                # lost the race or the window moved on; already journaled
                continue
            self._dispatch.mark_in_progress(attempt_id, now_epoch=now_epoch)
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
            results.append(self._record(pub, attempt_id, outcome, now_epoch))
        return results

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