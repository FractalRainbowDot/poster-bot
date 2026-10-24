"""Programmable offline ChannelTransport for tests (no network, no aiogram)."""

from __future__ import annotations

from dataclasses import dataclass, field

from poster_bot.domain.content import Content
from poster_bot.transport.ports import (
    NavEditRequest,
    PublishOutcome,
    Sent,
    TransportCallError,
)


@dataclass
class FakeTransport:
    """Records calls; hands out queued outcomes, defaulting to success.

    Each queued publish outcome is consumed by the next publish call in
    order; exceptions queued here are re-raised, simulating adapter bugs
    (the adapter itself must never raise).

    Navigation edits are an independent scripted channel of their own
    (``edit_script``/``queue_edit``): a queued outcome or exception applies
    only to ``edit_published_part``, so send-phase and edit-phase failures
    never entangle. Default edit success echoes the requested message id.
    """

    script: list[PublishOutcome | Exception] = field(default_factory=list)
    calls: list[Content] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    previews: list[int] = field(default_factory=list)
    owner_previews: list[Content] = field(default_factory=list)
    preview_script: list[PublishOutcome] = field(default_factory=list)
    edit_script: list[PublishOutcome | Exception] = field(default_factory=list)
    edit_calls: list[NavEditRequest] = field(default_factory=list)
    #: observer hook invoked with the request BEFORE the edit outcome is
    #: produced (tests inspect durable DB state mid-edit-phase)
    on_edit = None
    notify_fails: str | None = None  # code, or None for success
    _issued: int = 0  # channel ids handed out so far (no collisions)

    def queue(self, *outcomes: PublishOutcome | Exception) -> None:
        self.script.extend(outcomes)

    def queue_edit(self, *outcomes: PublishOutcome | Exception) -> None:
        self.edit_script.extend(outcomes)

    async def publish(self, content: Content) -> PublishOutcome:
        self.calls.append(content)
        if self.script:
            outcome = self.script.pop(0)
        else:
            # an album is physically N messages — one id per item, from a
            # cumulative band so ids never collide across calls (mirrors
            # the real adapter: send_media_group returns one per item)
            n = len(content.items) if content.kind == "album" else 1
            anchor = 9001 + self._issued
            self._issued += n
            outcome = Sent(
                message_ids=tuple(anchor + i for i in range(n)),
                permalink=f"https://t.me/c/test/{anchor + n - 1}",
            )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def preview_sent(self, message_id: int) -> PublishOutcome:
        self.previews.append(message_id)
        return Sent(message_ids=(message_id + 500_000,))

    async def send_preview(self, content: Content) -> PublishOutcome:
        # Owner-facing previews: recorded separately from channel publishes so
        # tests can assert the channel path was never invoked (PLAN §7).
        self.owner_previews.append(content)
        if self.preview_script:
            return self.preview_script.pop(0)
        n = len(content.items) if content.kind == "album" else 1
        if n > 1:
            # a previewed album is physically N messages — one id per item,
            # from a distinct band so ids never collide with singles
            base = 8_000_000 + sum(
                len(p.items) if p.kind == "album" else 1
                for p in self.owner_previews)
            return Sent(message_ids=tuple(base + i for i in range(n)),
                        permalink="")
        return Sent(message_ids=(7_000_000 + len(self.owner_previews),), permalink="")

    async def notify_owner(self, text: str) -> None:
        if self.notify_fails is not None:
            raise TransportCallError(self.notify_fails)
        self.notices.append(text)

    async def edit_published_part(self, request: NavEditRequest) -> PublishOutcome:
        # navigation edits are recorded apart from publishes so tests can
        # prove the body was never re-sent when only links are being edited
        self.edit_calls.append(request)
        if self.on_edit is not None:
            self.on_edit(request)
        if self.edit_script:
            outcome = self.edit_script.pop(0)
        else:
            outcome = Sent(
                message_ids=(request.message_id,),
                permalink=f"https://t.me/c/test/{request.message_id}",
            )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome