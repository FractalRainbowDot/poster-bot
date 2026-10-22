"""Best-effort in-place refresh, isolated from the channel publication path."""
from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

logger = logging.getLogger(__name__)


class DashboardPresentation:
    def __init__(self, flow, *, owner_id: int) -> None:
        self.flow = flow
        self.owner_id = owner_id
        self.navigation = flow.navigation_store(owner_id)
        # The router shares this lock: transition + presentation and refresh
        # cannot interleave, even with concurrent Dispatcher feed_update calls.
        self.lock = asyncio.Lock()
        self._frame = None
        self._message_id = None
        self._version = None
        self._pending = None
        self._retry_at = 0
        self._failures = 0

    def presented(self, reply) -> None:
        """Only a successfully sent explicit main card arms background edits."""
        self._frame = reply.dashboard
        self._version = reply.dashboard_version
        self._message_id = self.navigation.get(self.owner_id) if self._frame else None
        self._pending = None
        self._retry_at = self._failures = 0

    def _current(self) -> bool:
        return (self._frame is not None and self._version is not None
                and self.flow.dashboard_current(self._version)
                and self.navigation.get(self.owner_id) == self._message_id)

    def _invalidate(self) -> None:
        self._frame = self._pending = None
        self._message_id = self._version = None

    async def refresh(self, bot, *, now: int) -> None:
        async with self.lock:
            if not self._current():
                self._invalidate()
                return
            if now < self._retry_at:
                return
            try:
                frame = self.flow.dashboard_snapshot()
                if frame == self._frame:
                    self._pending = None
                    return
                if self._pending is None or self._pending.dashboard != frame:
                    self._pending = self.flow.dashboard_reply(now=now, snapshot=frame)
                # Synchronous snapshot/render cannot interleave with a handler.
                # Check again immediately before the only await/API mutation.
                if not self._current():
                    self._invalidate()
                    return
                from .handlers import _keyboard
                try:
                    await bot.edit_message_text(
                        chat_id=self.owner_id, message_id=self._message_id,
                        text=self._pending.text, reply_markup=_keyboard(self._pending),
                    )
                except TelegramBadRequest as exc:
                    # Telegram can have applied an edit whose response was lost.
                    # All other bad requests are terminal for this card: never
                    # resurrect missing/deleted messages with sendMessage.
                    if "message is not modified" not in exc.message.lower():
                        self._invalidate()
                        return
                except TelegramForbiddenError:
                    self._invalidate()
                    return
                if self._current():
                    self._frame = frame
                    self._pending = None
                    self._retry_at = self._failures = 0
            except Exception as exc:
                self._failures = min(self._failures + 1, 6)
                delay = min(60, 2 ** self._failures)
                if isinstance(exc, TelegramRetryAfter):
                    delay = max(delay, exc.retry_after)
                self._retry_at = now + delay
                logger.warning("dashboard refresh failed: class=%s", type(exc).__name__)
