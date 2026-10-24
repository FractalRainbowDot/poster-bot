"""Aiogram adapter for the owner flow — the thin edge of the UI.

Imported ONLY for live composition (never from the app import path; enforced
by ``tests/test_cli_offline.py``). This module owns exactly three concerns:

1. the owner/private guard — enforced BEFORE any flow call, so unauthorized
   traffic never triggers state changes, previews or callback execution and
   never receives any reply at all;
2. mapping Telegram incoming objects onto transport-free flow events
   (domain entities, media/forward intake, action tokens);
3. rendering ``Reply`` objects back into messages plus inline keyboards.

All business decisions live in ``ui.flow``; nothing here may duplicate them.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from aiogram import Bot, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from ..domain.content import ContentValidationError, Entity
from ..domain.planner import LogicalPost
from .flow import OwnerFlow
from .guards import is_owner_private
from .intake import forward_media_candidate
from .replies import TEXT_MAX, Reply
from .staged import Staged, copy_candidate_from_media, copy_candidate_from_text
from . import texts
from .presentation import present, delete_best_effort
from .dashboard_presentation import DashboardPresentation

#: clock injectable so offline composition/tests can pin time
Clock = Callable[[], int]


def _chat_type(chat: Any) -> str:
    value = getattr(chat.type, "value", chat.type)
    return str(value)


def to_domain_entities(entities: tuple[MessageEntity, ...] | None) -> tuple[Entity, ...]:
    """Map aiogram entities verbatim (custom emoji, language, mentions)."""

    if not entities:
        return ()
    mapped: list[Entity] = []
    for entity in entities:
        user = getattr(entity, "user", None)
        mapped.append(
            Entity(
                type=str(getattr(entity.type, "value", entity.type)),
                offset=entity.offset,
                length=entity.length,
                url=entity.url,
                user_id=user.id if user is not None else None,
                username=user.username if user is not None else None,
                custom_emoji_id=entity.custom_emoji_id,
                language=entity.language,
            )
        )
    return tuple(mapped)


def message_allowed(message: Message, *, owner_id: int) -> bool:
    """Guard gate for inbound messages (exported for offline tests)."""

    user = message.from_user
    if user is None or message.chat is None:
        return False
    return is_owner_private(
        chat_id=message.chat.id,
        chat_type=_chat_type(message.chat),
        user_id=user.id,
        owner_user_id=owner_id,
    )


def callback_allowed(query: CallbackQuery, *, owner_id: int) -> bool:
    """Guard gate for inbound callbacks (exported for offline tests)."""

    message = query.message
    user = query.from_user
    if user is None or message is None or getattr(message, "chat", None) is None:
        return False
    return is_owner_private(
        chat_id=message.chat.id,
        chat_type=_chat_type(message.chat),
        user_id=user.id,
        owner_user_id=owner_id,
    )


def _forward_source(message: Message) -> tuple[int, int] | None:
    """(source chat id, source message id) for a forward-as-is post.

    The bot can only reliably re-forward a copy it actually received, so the
    coordinates are those of the incoming message itself (the owner chat).
    This works for every forward origin kind, including hidden senders;
    Telegram preserves the original forward header on re-forward. Messages
    without a forward origin return None.
    """

    if message.forward_origin is None:
        return None
    if message.chat is None:
        return None
    return int(message.chat.id), int(message.message_id)


def _copy_candidate(message: Message, *, planned: bool = False) -> Staged:
    """Editable-copy parts of a forwarded message (media file ids are bot
    scoped but reusable; text copies verbatim)."""

    media_candidate = forward_media_candidate if planned else copy_candidate_from_media
    if message.photo:
        return media_candidate(
            "photo", message.photo[-1].file_id, message.caption,
            to_domain_entities(message.caption_entities),
            message.media_group_id,
        )
    if message.video:
        return media_candidate(
            "video", message.video.file_id, message.caption,
            to_domain_entities(message.caption_entities),
            message.media_group_id,
        )
    if message.audio:
        return media_candidate(
            "audio", message.audio.file_id, message.caption,
            to_domain_entities(message.caption_entities),
            message.media_group_id,
        )
    if message.text:
        if planned:
            return Staged(kind="logical", logical_copy=LogicalPost(
                body=message.text, entities=to_domain_entities(message.entities)))
        return copy_candidate_from_text(
            message.text, to_domain_entities(message.entities)
        )
    return Staged()


def _keyboard(reply: Reply) -> InlineKeyboardMarkup | None:
    if not reply.rows:
        return None
    rows = []
    for row in reply.rows:
        buttons = []
        for button in row:
            if button.url is not None:
                buttons.append(InlineKeyboardButton(text=button.label, url=button.url))
            else:
                # callback data is the server-issued token alone (13 bytes,
                # far below the 64-byte limit): no business data on clients
                buttons.append(
                    InlineKeyboardButton(text=button.label, callback_data=button.token)
                )
        rows.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _chunks(text: str) -> list[str]:
    """Defensive split for the 4096-char limit; the flow paginates lists so
    this normally returns a single chunk."""

    if len(text) <= TEXT_MAX:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= TEXT_MAX:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(line) > TEXT_MAX:
            parts.append(line[:TEXT_MAX])
            line = line[TEXT_MAX:]
        current = line
    if current:
        parts.append(current)
    return parts


def create_ui_router(
    flow: OwnerFlow, *, owner_id: int, clock: Clock | None = None,
    dashboard: DashboardPresentation | None = None,
) -> Router:
    """Router for the private owner chat. Include it on the bot ONLY in live
    composition; ``flow`` must be composed on the same connection/services."""

    now_fn: Clock = clock if clock is not None else (lambda: int(time.time()))
    router = Router(name="owner_ui")
    navigation = flow.navigation_store(owner_id)
    dashboard = dashboard or DashboardPresentation(flow, owner_id=owner_id)
    presentation_lock = dashboard.lock

    async def serialized(handler, event, data):
        # Keep the committed flow transition and its service-card rendering in
        # order, including when a Dispatcher processes updates concurrently.
        async with presentation_lock:
            return await handler(event, data)

    router.message.outer_middleware(serialized)
    router.callback_query.outer_middleware(serialized)

    async def deliver(bot: Bot, chat_id: int, reply: Reply) -> Reply:
        reply, preview_complete = await flow.review_reply(reply, now=now_fn())
        if reply.stale_callback:
            return reply
        parts = _chunks(reply.text)
        for part in parts[:-1]:
            await bot.send_message(chat_id=chat_id, text=part)
        try:
            await present(bot, chat_id, parts[-1], _keyboard(reply), navigation,
                          now=now_fn())
        except Exception:
            # Acknowledged preview parts remain durably known but may never
            # replace the last complete stack without their fresh controls.
            flow.review_delivery_failed(reply, now=now_fn())
            raise
        dashboard.presented(reply)
        try:
            if preview_complete:
                navigation.lifecycle.presented(keep_preview=flow.preview_context(), now=now_fn())
            await navigation.lifecycle.cleanup(
                bot, chat_id, now=now_fn(),
                protected={navigation.get(chat_id), navigation.keyboard_message(chat_id)},
                source_referenced=flow.forward_source_referenced,
            )
        except Exception:
            pass  # cosmetic cleanup must never replay a business operation
        return reply

    def allowed(message: Message) -> bool:
        return message_allowed(message, owner_id=owner_id)

    @router.message(lambda message: texts.MENU_ALIASES.get(message.text) == texts.MENU_RESTART
                    and message.forward_origin is None)
    @router.message(Command("cancel"), lambda message: message.forward_origin is None)
    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        if not allowed(message):
            return
        if texts.MENU_ALIASES.get(message.text) == texts.MENU_RESTART:
            reply = await flow.menu_navigation(label=texts.MENU_RESTART, now=now_fn())
        else:
            reply = await flow.command_start(now=now_fn())
        # Telegram cannot combine reply and inline keyboards on one message.
        # Reinstall on every /start or Рестарт: local history clearing is invisible to us.
        # Only our durably tracked prompt is eligible for replacement cleanup.
        prompt = await message.bot.send_message(
            chat_id=message.chat.id, text="Меню доступно под полем ввода.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text=label) for label in row]
                          for row in texts.BOTTOM_MENU_ROWS],
                resize_keyboard=True, is_persistent=True))
        try:
            navigation.record_keyboard(message.chat.id, prompt.message_id, now=now_fn())
        except Exception:
            await delete_best_effort(message.bot, message.chat.id, prompt.message_id)
            raise
        await deliver(message.bot, message.chat.id, reply)
        await delete_best_effort(message.bot, message.chat.id, message.message_id)

    @router.message()
    async def on_message(message: Message) -> None:
        if not allowed(message):
            return  # unauthorized traffic gets no reply at all
        now = now_fn()
        if (message.text in texts.MENU_ALIASES
                and message.forward_origin is None):
            reply = await flow.menu_navigation(label=message.text, now=now)
            await deliver(message.bot, message.chat.id, reply)
            await delete_best_effort(message.bot, message.chat.id, message.message_id)
            return
        # durable per-message effect key: a redelivered update (crash between
        # the flow's committed effect and inbox.complete) replays idempotently
        # inside the flow instead of duplicating intake effects
        source_key = f"{message.chat.id}:{message.message_id}"
        discardable = flow.discardable_context()

        async def finish_input(reply):
            reply = await deliver(message.bot, message.chat.id, reply)
            # No receipt on failed business/state writes or failed guidance delivery.
            # Content/forward capture states are never eligible for rejection purge.
            if discardable and not flow.input_finished(source_key):
                flow.discard_input(source_key, now=now)
            if flow.input_finished(source_key) and not reply.retain_input:
                await delete_best_effort(message.bot, message.chat.id, message.message_id)

        if flow.input_discarded(source_key):
            await finish_input(flow.discarded_input_reply(now=now))
            return
        # service messages carry no sender and are dropped silently by the
        # guard above (flow.service_message stays the documented no-op)
        forwarded_from = _forward_source(message)
        if forwarded_from is not None:
            try:
                copy_candidate = _copy_candidate(message, planned=flow._planned())
                if copy_candidate.empty:
                    raise ContentValidationError("unsupported forwarded content")
            except ContentValidationError:
                await deliver(message.bot, message.chat.id,
                              flow.forward_rejected_reply(now=now))
                if flow.discard_rejected_forward(chat_id=message.chat.id,
                                                message_id=message.message_id,
                                                now=now):
                    await delete_best_effort(message.bot, message.chat.id,
                                             message.message_id)
                return
            reply = await flow.forwarded(
                now=now,
                src_chat_id=forwarded_from[0],
                src_message_id=forwarded_from[1],
                media_group_id=message.media_group_id,
                copy_candidate=copy_candidate,
                source_key=source_key,
            )
            await deliver(message.bot, message.chat.id, reply)
            if (reply.rejected_forward and not reply.retain_input
                    and flow.discard_rejected_forward(
                        chat_id=message.chat.id, message_id=message.message_id, now=now)):
                await delete_best_effort(message.bot, message.chat.id, message.message_id)
            return
        if message.text is not None:
            reply = await flow.text(
                now=now,
                text=message.text,
                entities=to_domain_entities(message.entities),
                source_key=source_key,
            )
            await finish_input(reply)
            return
        media = None
        if message.photo:
            media = ("photo", message.photo[-1].file_id)
        elif message.video:
            media = ("video", message.video.file_id)
        elif message.audio:
            media = ("audio", message.audio.file_id)
        if media is not None:
            reply = await flow.media(
                now=now,
                kind=media[0],
                file_id=media[1],
                caption=message.caption,
                caption_entities=to_domain_entities(message.caption_entities),
                media_group_id=message.media_group_id,
                source_key=source_key,
            )
            await finish_input(reply)
            return
        reply = flow.unsupported(now=now)
        await finish_input(reply)

    @router.callback_query()
    async def on_callback(query: CallbackQuery) -> None:
        message = query.message
        if not callback_allowed(query, owner_id=owner_id) or message is None:
            # forged callbacks: executed nowhere, acknowledged nowhere
            return
        # Unknown/superseded cards cannot consume tokens or target previews.
        if navigation.get(message.chat.id) != message.message_id:
            try:
                await query.answer(texts.BUTTON_STALE)
            except Exception:
                pass
            return
        answered = False

        async def acknowledge() -> None:
            nonlocal answered
            answered = True
            try:
                await query.answer()
            except Exception:
                pass  # an expired query must not swallow the state update

        reply = await flow.press(now=now_fn(), token=query.data or "",
                                 on_consumed=acknowledge, refresh_now=now_fn)
        if not answered:
            try:
                await query.answer(reply.text if reply.stale_callback else None)
            except Exception:
                pass
        if reply.stale_callback:
            return  # even a failed toast must leave the active card untouched
        await deliver(query.bot, message.chat.id, reply)
        for chat_id, message_id in reply.released_inputs:
            if (chat_id == owner_id and flow.input_captured(f"{chat_id}:{message_id}")
                    and not flow.forward_source_referenced(
                        chat_id=chat_id, message_id=message_id)):
                await delete_best_effort(query.bot, chat_id, message_id)

    return router
