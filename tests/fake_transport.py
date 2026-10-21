"""Programmable offline ChannelTransport for tests (no network, no aiogram)."""

from __future__ import annotations

from dataclasses import dataclass, field

from poster_bot.domain.content import Content
from poster_bot.transport.ports import (
    PublishOutcome,
    Sent,
    TransportCallError,
)


@dataclass
class FakeTransport:
    """Records calls; hands out queued outcomes, defaulting to success.

    Each queued outcome is consumed by the next publish call in order;
    exceptions queued here are re-raised, simulating adapter bugs (the
    adapter itself must never raise).
    """

    script: list[PublishOutcome | Exception] = field(default_factory=list)
    calls: list[Content] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    previews: list[int] = field(default_factory=list)
    owner_previews: list[Content] = field(default_factory=list)
    preview_script: list[PublishOutcome] = field(default_factory=list)
    notify_fails: str | None = None  # code, or None for success

    def queue(self, *outcomes: PublishOutcome | Exception) -> None:
        self.script.extend(outcomes)

    async def publish(self, content: Content) -> PublishOutcome:
        self.calls.append(content)
        if self.script:
            outcome = self.script.pop(0)
        else:
            anchor = 9000 + len(self.calls)
            outcome = Sent(
                message_ids=(anchor,),
                permalink=f"https://t.me/c/test/{anchor}",
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
        return Sent(message_ids=(7_000_000 + len(self.owner_previews),), permalink="")

    async def notify_owner(self, text: str) -> None:
        if self.notify_fails is not None:
            raise TransportCallError(self.notify_fails)
        self.notices.append(text)