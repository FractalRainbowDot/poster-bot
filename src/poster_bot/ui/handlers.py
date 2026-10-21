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
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
)

from ..domain.content import ContentValidationError, Entity
from .flow import OwnerFlow
from .guards import is_owner_private
from .replies import TEXT_MAX, Reply
from .staged import Staged, copy_candidate_from_media, copy_candidate_from_text

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


def _copy_candidate(message: Message) -> Staged:
    """Editable-copy parts of a forwarded message (media file ids are bot
    scoped but reusable; text copies verbatim)."""

    if message.photo:
        return copy_candidate_from_media(
            "photo", message.photo[-1].file_id, message.caption,
            to_domain_entities(message.caption_entities),
            message.media_group_id,
        )
    if message.video:
        return copy_candidate_from_media(
            "video", message.video.file_id, message.caption,
            to_domain_entities(message.caption_entities),
            message.media_group_id,
        )
    if message.audio:
        return copy_candidate_from_media(
            "audio", message.audio.file_id, message.caption,
            to_domain_entities(message.caption_entities),
            message.media_group_id,
        )
    if message.text:
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


async def _deliver(bot: Bot, chat_id: int, reply: Reply) -> None:
    parts = _chunks(reply.text)
    for index, part in enumerate(parts):
        markup = _keyboard(reply) if index == len(parts) - 1 else None
        await bot.send_message(chat_id=chat_id, text=part, reply_markup=markup)


def create_ui_router(
    flow: OwnerFlow, *, owner_id: int, clock: Clock | None = None
) -> Router:
    """Router for the private owner chat. Include it on the bot ONLY in live
    composition; ``flow`` must be composed on the same connection/services."""

    now_fn: Clock = clock if clock is not None else (lambda: int(time.time()))
    router = Router(name="owner_ui")

    def allowed(message: Message) -> bool:
        return message_allowed(message, owner_id=owner_id)

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        if not allowed(message):
            return
        reply = await flow.command_start(now=now_fn())
        await _deliver(message.bot, message.chat.id, reply)

    @router.message()
    async def on_message(message: Message) -> None:
        if not allowed(message):
            return  # unauthorized traffic gets no reply at all
        now = now_fn()
        # durable per-message effect key: a redelivered update (crash between
        # the flow's committed effect and inbox.complete) replays idempotently
        # inside the flow instead of duplicating intake effects
        source_key = f"{message.chat.id}:{message.message_id}"
        # service messages carry no sender and are dropped silently by the
        # guard above (flow.service_message stays the documented no-op)
        forwarded_from = _forward_source(message)
        if forwarded_from is not None:
            try:
                copy_candidate = _copy_candidate(message)
            except ContentValidationError as exc:
                reply = flow.content_rejected(now=now, exc=exc)
                await _deliver(message.bot, message.chat.id, reply)
                return
            reply = await flow.forwarded(
                now=now,
                src_chat_id=forwarded_from[0],
                src_message_id=forwarded_from[1],
                media_group_id=message.media_group_id,
                copy_candidate=copy_candidate,
                source_key=source_key,
            )
            await _deliver(message.bot, message.chat.id, reply)
            return
        if message.text is not None:
            reply = await flow.text(
                now=now,
                text=message.text,
                entities=to_domain_entities(message.entities),
                source_key=source_key,
            )
            await _deliver(message.bot, message.chat.id, reply)
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
            await _deliver(message.bot, message.chat.id, reply)
            return
        reply = flow.unsupported(now=now)
        await _deliver(message.bot, message.chat.id, reply)

    @router.callback_query()
    async def on_callback(query: CallbackQuery) -> None:
        message = query.message
        if not callback_allowed(query, owner_id=owner_id) or message is None:
            # forged callbacks: executed nowhere, acknowledged nowhere
            return
        reply = await flow.press(now=now_fn(), token=query.data or "")
        try:
            await query.answer()
        except Exception:
            pass  # an expired query toast must not swallow the state update
        await _deliver(query.bot, message.chat.id, reply)

    return router