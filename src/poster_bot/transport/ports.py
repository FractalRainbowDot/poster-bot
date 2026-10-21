"""Transport ports: what the sender needs from the channel, nothing else.

Outcome taxonomy matches ``journal.SEND_CODES``:
- ``Sent``                 → message is verifiably in the channel;
- ``DefinitiveFailure``    → channel rejected the call (4xx/rate limit); no
  delivery happened, and per PLAN there is no automatic publish retry;
- ``UncertainOutcome``     → delivery state unknowable (timeout, network,
  partial album/forward): the publication parks in ``uncertain``.

The adapter never raises for publish results; failures are outcomes.
Only ``notify_owner`` raises ``TransportCallError`` (the notices outbox
keeps the notice pending and retries delivery at runtime level).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from poster_bot.domain.content import Content


@dataclass(frozen=True)
class Sent:
    """The channel confirmed delivery.

    ``message_ids`` are the channel messages produced (one for single posts,
    several for albums/forwards); ``permalink`` is the channel link stored for
    the owner (empty for previews, which are not channel posts).
    """

    message_ids: tuple[int, ...]
    permalink: str = ""


@dataclass(frozen=True)
class DefinitiveFailure:
    """Rejected by the channel: ``api_error`` or ``rate_limited``."""

    code: str


@dataclass(frozen=True)
class UncertainOutcome:
    """Delivery cannot be established: ``timeout``, ``transport_error`` or
    ``partial_result`` (album/forward delivered fewer items than requested)."""

    code: str


PublishOutcome = Sent | DefinitiveFailure | UncertainOutcome


class TransportCallError(Exception):
    """Owner-notice/preview call failed; carries only an allowlisted code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@runtime_checkable
class ChannelTransport(Protocol):
    """Narrow channel boundary used by the sender and runtime composition.

    ``publish`` sends ``content`` to the configured channel and returns an
    outcome without raising (any adapter-level crash arrives as an
    ``UncertainOutcome`` from the adapter itself).

    ``preview_sent`` privately copies a channel message to the owner chat so
    the owner can verify published formatting; it mirrors the publish outcome
    shape because a failed copy is equally ambiguous over the wire.

    ``notify_owner`` sends plain text to the owner; failures raise
    ``TransportCallError`` so callers can keep outbox items pending.

    Implementations must never raise from ``publish``/``preview_sent``: every
    channel-level failure arrives as a classified outcome.
    """

    async def publish(self, content: "Content") -> PublishOutcome: ...

    async def preview_sent(self, message_id: int) -> PublishOutcome: ...

    async def notify_owner(self, text: str) -> None: ...