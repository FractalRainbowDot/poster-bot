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
    from poster_bot.domain.content import Content, Entity


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
    # Trusted positive destination IDs acknowledged by the actual API result,
    # even when that result cannot establish complete delivery. Cleanup evidence
    # only: these IDs never turn an uncertain publication into a successful one.
    confirmed_message_ids: tuple[int, ...] = ()


PublishOutcome = Sent | DefinitiveFailure | UncertainOutcome

#: navigation-edit kinds: the two Bot API calls a published part can be
#: re-rendered with (message text, or a media/album caption). Never anything
#: else, and never a raw Telegram method name.
NAV_EDIT_KINDS = ("text", "caption")


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


@dataclass(frozen=True)
class NavEditRequest:
    """One post-publish navigation edit of an ALREADY CONFIRMED message.

    Produced only by the sender's navigation pass, exclusively from durable
    part receipts (target id) and the frozen plan snapshot (text/entities):
    callback data and ambient config can never reach a channel edit.

    ``text`` is the part's final published text byte-for-byte — the edit only
    adds 'text_link' entities over nav-footer labels, it never rewrites the
    body. ``kind`` selects the Bot API call: ``text`` → editMessageText on a
    text part, ``caption`` → editMessageCaption on a media/album part (the
    caption rides the group's FIRST item id).

    The request carries no chat id: implementations bind every edit to the
    one resolved channel destination they were composed with.
    """

    message_id: int
    text: str
    entities: "tuple[Entity, ...]"
    kind: str

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or not isinstance(
                self.message_id, int) or self.message_id <= 0:
            raise ValueError("navigation edit needs a positive message id")
        if not isinstance(self.text, str):
            raise ValueError("navigation edit needs text")
        if self.kind not in NAV_EDIT_KINDS:
            raise ValueError(
                f"navigation edit kind must be one of {NAV_EDIT_KINDS}")


@runtime_checkable
class ChannelEditor(Protocol):
    """Separate capability port for post-publish navigation edits.

    Deliberately NOT part of ``ChannelTransport``: every transport stub and
    the channel publisher stay valid, and only implementations that really
    edit channel messages satisfy (and are used as) editors.

    ``edit_published_part`` applies ``request`` to the bound channel and
    returns an outcome without raising, mirroring ``publish``: ``Sent``
    confirms the edit (its ``message_ids`` identify the edited message),
    ``DefinitiveFailure`` means the channel rejected it (e.g. message gone),
    ``UncertainOutcome`` means the outcome is unknowable. The caller records
    the outcome durably; nothing here ever retries."""

    async def edit_published_part(
        self, request: "NavEditRequest"
    ) -> PublishOutcome: ...
