"""Aiogram-backed channel transport.

Imported ONLY for live composition (never from the app import path; enforced
by ``tests/test_cli_offline.py``). Bound to the configured channel and owner:
all sends go to the channel, previews/notices go to the owner only (PLAN §7).

Retry policy (PLAN §6): aiogram 3.31 performs a single HTTP request per call
and ships no retry middleware; we construct the session explicitly with a
conservative timeout and register no middlewares, so no call can ever be
replayed into a duplicate channel send.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import (
    InputMediaPhoto,
    InputMediaVideo,
    MessageEntity,
    User,
)

from poster_bot.config import ENV_CHANNEL, Config, ConfigError
from poster_bot.domain.content import Content, Entity
from poster_bot.transport.ports import (
    ChannelTransport,
    DefinitiveFailure,
    PublishOutcome,
    Sent,
    TransportCallError,
    UncertainOutcome,
)

# Conservative explicit timeout: a hung Telegram call parks as uncertain
# instead of blocking the runtime for the aiogram default (60s).
SENDER_TIMEOUT_SECONDS = 15.0


def create_bot(config: Config) -> Bot:
    """Bot with an explicit session timeout and NO retry middleware."""

    session = AiohttpSession(timeout=SENDER_TIMEOUT_SECONDS)
    # No middleware is registered: aiogram's default manager executes exactly
    # one request per call. Never add RetryMiddleware here — replays would
    # risk duplicate channel posts (PLAN §6).
    return Bot(token=config.bot_token, session=session)


def _user_for(entity: Entity) -> User:
    """Rebuild the Telegram user object for mention/text_mention entities.

    Telegram only requires a display identity here; bot-authored mentions
    carry the referenced user's id. ``first_name`` falls back to the stored
    username (the owner-supplied display name we keep) and then to the id.
    """

    display = entity.username or f"user{entity.user_id}"
    return User(
        id=entity.user_id,
        is_bot=False,
        first_name=display,
        username=entity.username,
    )


def to_aiogram_entities(entities: tuple[Entity, ...] | None) -> tuple[MessageEntity, ...] | None:
    """Map domain entities onto aiogram ones.

    Offsets/lengths pass through unchanged: both sides count UTF-16 code
    units (validated in ``domain.content``), and aiogram serializes them
    without re-encoding.
    """

    if not entities:
        return None
    mapped: list[MessageEntity] = []
    for entity in entities:
        mapped.append(
            MessageEntity(
                type=entity.type,
                offset=entity.offset,
                length=entity.length,
                url=entity.url,
                language=entity.language,
                custom_emoji_id=entity.custom_emoji_id,
                user=_user_for(entity) if entity.user_id is not None else None,
            )
        )
    return tuple(mapped)


class AiogramChannelTransport(ChannelTransport):
    """Channel-bound publisher over a single aiogram ``Bot``.

    The ``bot`` parameter is duck-typed (any object exposing the used
    coroutine methods), which lets tests exercise this exact adapter code
    offline with a stub.

    ``channel_id`` is the RESOLVED numeric destination: a configured
    ``@username`` must already have been resolved to its stable numeric chat
    id (``poster_bot.live.resolve_channel``) before composition, so no send
    can ever silently retarget by username. ``channel_username`` (bare, no
    leading '@') only improves permalink building for public channels; sends
    never use it.
    """

    def __init__(
        self,
        bot: Bot,
        *,
        channel_id: int,
        owner_id: int,
        channel_username: str | None = None,
    ) -> None:
        if isinstance(channel_id, bool) or not isinstance(channel_id, int) or channel_id == 0:
            raise ValueError(
                "channel_id must be a resolved non-zero numeric chat id"
            )
        self._bot = bot
        self._channel_id = channel_id
        self._owner_id = owner_id
        username = (channel_username or "").strip().removeprefix("@").strip()
        self._channel_username = username or None

    # -- publish ---------------------------------------------------------

    def _permalink(
        self, anchor_message_id: int, *, chat_id: int | None = None, username: str | None = None
    ) -> str:
        """Channel permalink for delivered messages.

        Public channels link through the username (the delivered chat's own
        username wins, else the configured one); private channels use the
        ``t.me/c/<internal-id>/<message>`` form built from the -100 chat id.
        A configured '@username' target must never leak into a t.me/c path."""

        if username:
            return f"https://t.me/{username}/{anchor_message_id}"
        if self._channel_username:
            return f"https://t.me/{self._channel_username}/{anchor_message_id}"
        chat = self._channel_id if chat_id is None else chat_id
        return f"https://t.me/c/{str(abs(chat)).removeprefix('100')}/{anchor_message_id}"

    def _sent_single(self, message) -> Sent:
        chat = getattr(message, "chat", None)
        return Sent(
            message_ids=(message.message_id,),
            permalink=self._permalink(
                message.message_id,
                chat_id=getattr(chat, "id", None),
                username=getattr(chat, "username", None),
            ),
        )

    def _sent_group(self, messages) -> Sent:
        ids = tuple(m.message_id for m in messages)
        chat = getattr(messages[0], "chat", None) if messages else None
        return Sent(
            message_ids=ids,
            permalink=self._permalink(
                max(ids),
                chat_id=getattr(chat, "id", None),
                username=getattr(chat, "username", None),
            ),
        )

    async def publish(self, content: Content) -> PublishOutcome:
        try:
            return await self._publish(content, self._channel_id)
        except Exception as exc:  # conservative classification, never raise
            return self._classify(exc)

    async def send_preview(self, content: Content) -> PublishOutcome:
        """Deliver a preview copy of *content* into the private owner chat.

        This is the only owner-facing content path in the publisher: channel
        publishing stays bound to the configured channel and previews never
        retarget it (PLAN §7). Previews are not channel posts, so the Sent
        outcome carries an empty permalink. Never raises — a failed preview
        surfaces to the flow as a non-Sent outcome, not as an outage.
        """

        try:
            outcome = await self._publish(content, self._owner_id)
        except Exception as exc:
            return self._classify(exc)
        if isinstance(outcome, Sent):
            return Sent(message_ids=outcome.message_ids, permalink="")
        return outcome

    async def _publish(self, content: Content, target: int) -> PublishOutcome:
        match content.kind:
            case "text":
                message = await self._bot.send_message(
                    chat_id=target,
                    text=content.text,
                    entities=to_aiogram_entities(content.entities),
                )
                return self._sent_single(message)
            case "photo":
                message = await self._bot.send_photo(
                    chat_id=target,
                    photo=content.media.file_id,
                    caption=content.media.caption,
                    caption_entities=to_aiogram_entities(
                        content.media.caption_entities
                    ),
                )
                return self._sent_single(message)
            case "video":
                message = await self._bot.send_video(
                    chat_id=target,
                    video=content.media.file_id,
                    caption=content.media.caption,
                    caption_entities=to_aiogram_entities(
                        content.media.caption_entities
                    ),
                )
                return self._sent_single(message)
            case "audio":
                # Bot API media groups require at least two items.
                message = await self._bot.send_audio(
                    chat_id=target,
                    audio=content.media.file_id,
                    caption=content.media.caption,
                    caption_entities=to_aiogram_entities(
                        content.media.caption_entities
                    ),
                )
                return self._sent_single(message)
            case "album":
                caption_entities = to_aiogram_entities(content.caption_entities)
                media = []
                for index, item in enumerate(content.items):
                    if index == 0 and content.caption is not None:
                        # album-level caption rides the first item
                        caption: str | None = content.caption
                        item_entities = caption_entities
                    else:
                        # per-item captions are carried verbatim on each item
                        caption = item.caption
                        item_entities = to_aiogram_entities(
                            item.caption_entities
                        )
                    match item.kind:
                        case "photo":
                            media.append(
                                InputMediaPhoto(
                                    media=item.file_id,
                                    caption=caption,
                                    caption_entities=item_entities,
                                )
                            )
                        case "video":
                            media.append(
                                InputMediaVideo(
                                    media=item.file_id,
                                    caption=caption,
                                    caption_entities=item_entities,
                                )
                            )
                messages = await self._bot.send_media_group(
                    chat_id=target, media=media
                )
                return self._sent_group(messages)
            case "forward":
                requested = list(content.forward.message_ids)
                returned = await self._bot.forward_messages(
                    chat_id=target,
                    from_chat_id=content.forward.chat_id,
                    message_ids=requested,
                )
                if len(returned) < len(requested):
                    # Telegram silently skips unavailable sources: a partial
                    # album cannot be auto-fixed, so it parks for a human.
                    return UncertainOutcome(code="partial_result")
                return self._sent_group(returned)
            case unknown:  # unreachable through domain validation
                raise AssertionError(f"unhandled content kind {unknown!r}")

    # -- preview / notice --------------------------------------------------

    async def preview_sent(self, message_id: int) -> PublishOutcome:
        try:
            copied = await self._bot.copy_messages(
                chat_id=self._owner_id,
                from_chat_id=self._channel_id,
                message_ids=[message_id],
            )
        except Exception as exc:
            return self._classify(exc)
        if not copied:
            return UncertainOutcome(code="partial_result")
        return Sent(message_ids=(copied[0].message_id,))

    async def notify_owner(self, text: str) -> None:
        try:
            await self._bot.send_message(chat_id=self._owner_id, text=text)
        except Exception as exc:
            outcome = self._classify(exc)
            code = outcome.code if isinstance(outcome, (DefinitiveFailure, UncertainOutcome)) else "transport_error"
            raise TransportCallError(code) from None

    # -- classification ----------------------------------------------------

    def _classify(self, exc: Exception) -> PublishOutcome:
        """Map failures onto ``journal.SEND_CODES`` without leaking details."""

        match exc:
            case asyncio.TimeoutError():
                return UncertainOutcome(code="timeout")
            case TelegramRetryAfter():
                # A 429 means "come back later"; auto-waiting/retrying a send
                # is forbidden (PLAN §6), so this parks the publication.
                return DefinitiveFailure(code="rate_limited")
            case TelegramNetworkError() | TelegramServerError() | ClientDecodeError():
                return UncertainOutcome(code="transport_error")
            case TelegramAPIError():
                return DefinitiveFailure(code="api_error")
            case _:
                return UncertainOutcome(code="transport_error")


def build_transport(config: Config, *, bot: Bot | None = None) -> AiogramChannelTransport:
    """Compose the channel-bound transport for live runtime use.

    A ``@username`` target must be resolved BEFORE transport composition
    (async ``poster_bot.live.resolve_channel``): this synchronous helper
    never guesses a destination and refuses string targets outright."""

    if isinstance(config.channel_id, str):
        raise ConfigError([
            f"{ENV_CHANNEL} must be resolved to its numeric channel id "
            "before transport composition"
        ])
    return AiogramChannelTransport(
        bot if bot is not None else create_bot(config),
        channel_id=config.channel_id,
        owner_id=config.owner_user_id,
    )
