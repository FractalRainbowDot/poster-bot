"""The real aiogram adapter driven offline by a duck-typed stub bot.

No network, no aiogram session: the stub stands in for aiogram's Bot methods
(same names/return shapes), so adapter logic — payload mapping, count
checking, conservative error classification — is exercised for real.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage

from conftest import text_content
from poster_bot.domain.content import (
    Content,
    Entity,
    ForwardSource,
    MediaRef,
)
from poster_bot.transport.aiogram_channel import (
    SENDER_TIMEOUT_SECONDS,
    AiogramChannelTransport,
    build_transport,
    create_bot,
    to_aiogram_entities,
)
from poster_bot.transport.ports import (
    DefinitiveFailure,
    NavEditRequest,
    Sent,
    TransportCallError,
    UncertainOutcome,
)

CHANNEL = -1001234567890
OWNER = 4242


def run(coro):
    return asyncio.run(coro)


def message(message_id: int, *, username: str | None = None):
    return SimpleNamespace(
        message_id=message_id,
        chat=SimpleNamespace(id=CHANNEL, username=username),
    )


def message_id(message_id: int):
    return SimpleNamespace(message_id=message_id)


class StubBot:
    def __init__(self, *, result=None, raises: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.result = result
        self.raises = raises

    def _respond(self, name: str, kwargs):
        self.calls.append((name, kwargs))
        if self.raises is not None:
            raise self.raises
        if callable(self.result):
            return self.result(kwargs)
        return self.result

    async def send_message(self, **kwargs):
        return self._respond("send_message", kwargs)

    async def edit_message_text(self, **kwargs):
        return self._respond("edit_message_text", kwargs)

    async def edit_message_caption(self, **kwargs):
        return self._respond("edit_message_caption", kwargs)

    async def send_photo(self, **kwargs):
        return self._respond("send_photo", kwargs)

    async def send_video(self, **kwargs):
        return self._respond("send_video", kwargs)

    async def send_audio(self, **kwargs):
        return self._respond("send_audio", kwargs)

    async def send_media_group(self, **kwargs):
        return self._respond("send_media_group", kwargs)

    async def forward_messages(self, **kwargs):
        return self._respond("forward_messages", kwargs)

    async def copy_messages(self, **kwargs):
        return self._respond("copy_messages", kwargs)


def transport(bot) -> AiogramChannelTransport:
    return AiogramChannelTransport(bot, channel_id=CHANNEL, owner_id=OWNER)


class TestSingleSends:
    def test_text_sent_private_channel_permalink(self):
        bot = StubBot(result=message(7))
        outcome = run(transport(bot).publish(text_content("hello")))
        assert outcome == Sent(message_ids=(7,),
                               permalink="https://t.me/c/1234567890/7")
        name, kwargs = bot.calls[0]
        assert name == "send_message" and kwargs["chat_id"] == CHANNEL

    def test_public_channel_uses_username_permalink(self):
        bot = StubBot(result=message(7, username="my_news"))
        outcome = run(transport(bot).publish(text_content("hello")))
        assert outcome.permalink == "https://t.me/my_news/7"

    def test_photo_maps_file_id_and_caption_entities(self):
        bot = StubBot(result=message(8))
        content = Content(
            kind="photo",
            media=MediaRef(
                "photo",
                "ph1",
                caption="see this",
                caption_entities=(
                    Entity(type="text_mention", offset=0, length=3,
                           user_id=9, username="bob"),
                ),
            ),
        )
        outcome = run(transport(bot).publish(content))
        assert isinstance(outcome, Sent)
        _, kwargs = bot.calls[0]
        assert kwargs["photo"] == "ph1" and kwargs["caption"] == "see this"
        ent = kwargs["caption_entities"][0]
        assert ent.user.id == 9 and ent.user.first_name == "bob"

    def test_video_uses_video_api(self):
        bot = StubBot(result=message(9))
        content = Content(kind="video", media=MediaRef("video", "vd1"))
        assert isinstance(run(transport(bot).publish(content)), Sent)
        assert bot.calls[0][0] == "send_video"

    @pytest.mark.parametrize("preview", [False, True])
    def test_single_audio_uses_send_audio_not_invalid_one_item_group(self, preview):
        bot = StubBot(result=message(10))
        content = Content(kind="audio", media=MediaRef(
            "audio", "au1", caption="mp3",
            caption_entities=(Entity("bold", 0, 3),)))
        adapter = transport(bot)
        outcome = run(adapter.send_preview(content) if preview else adapter.publish(content))
        assert outcome == Sent((10,), "" if preview else "https://t.me/c/1234567890/10")
        assert len(bot.calls) == 1
        name, payload = bot.calls[0]
        assert name == "send_audio"
        assert payload["chat_id"] == (OWNER if preview else CHANNEL)
        assert payload["audio"] == "au1" and payload["caption"] == "mp3"
        assert payload["caption_entities"][0].type == "bold"


class TestAlbumAndForward:
    @pytest.mark.parametrize('result,known', [
        ([message_id(71)], (71,)),
        ([message_id(71), message_id(72), message_id(73)], (71, 72, 73)),
        ([message_id(71), message_id(71)], (71,)),
        ([message_id(True), message_id(72)], (72,)),
        ([message_id(-1), message_id('72')], ()),
        (None, ()),
        ({'message_id': 71}, ()),
    ])
    @pytest.mark.parametrize('preview', [False, True])
    def test_partial_native_ack_retains_only_confirmed_destination_ids(self, result, known, preview):
        bot = StubBot(result=result)
        adapter = transport(bot)
        content = Content(kind='forward', mode='forward', forward=ForwardSource(OWNER, (40, 41)))
        outcome = run(adapter.send_preview(content) if preview else adapter.publish(content))
        assert isinstance(outcome, UncertainOutcome)
        assert outcome.code == 'partial_result'
        assert outcome.confirmed_message_ids == known
        assert len(bot.calls) == 1
        assert bot.calls[0][1]['chat_id'] == (OWNER if preview else CHANNEL)

    def test_album_single_caption_on_first_item(self):
        bot = StubBot(result=[message(20), message(21)])
        content = Content(
            kind="album",
            items=(MediaRef("photo", "p1"), MediaRef("video", "v1")),
            caption="album text",
        )
        outcome = run(transport(bot).publish(content))
        assert outcome == Sent(message_ids=(20, 21),
                               permalink="https://t.me/c/1234567890/21")
        media = bot.calls[0][1]["media"]
        assert media[0].caption == "album text"
        assert media[1].caption is None  # album-level caption rides item 1

    def test_album_per_item_captions_each_on_own_item(self):
        # distinct per-item captions are carried verbatim to the API: each
        # InputMedia gets its own caption and entities, nothing is dropped
        bot = StubBot(result=[message(22), message(23)])
        content = Content(
            kind="album",
            items=(
                MediaRef("photo", "p1", caption="первая",
                         caption_entities=(Entity("bold", 0, 6),)),
                MediaRef("video", "v1", caption="вторая",
                         caption_entities=(Entity("italic", 0, 6),)),
            ),
        )
        outcome = run(transport(bot).publish(content))
        assert isinstance(outcome, Sent)
        media = bot.calls[0][1]["media"]
        assert media[0].caption == "первая"
        assert media[1].caption == "вторая"
        assert media[0].caption_entities[0].type == "bold"
        assert media[1].caption_entities[0].type == "italic"

    def test_forward_count_match_is_sent(self):
        bot = StubBot(result=[message_id(30), message_id(31)])
        content = Content(kind="forward", mode="forward",
                          forward=ForwardSource(chat_id=-100777, message_ids=(5, 6)))
        outcome = run(transport(bot).publish(content))
        assert isinstance(outcome, Sent)
        _, kwargs = bot.calls[0]
        assert kwargs["from_chat_id"] == -100777
        assert list(kwargs["message_ids"]) == [5, 6]

    def test_forward_short_count_is_partial_uncertain(self):
        # Telegram silently skips unavailable sources: fewer returned items
        # means an incomplete post that no bot may auto-fix.
        bot = StubBot(result=[message_id(30)])
        content = Content(kind="forward", mode="forward",
                          forward=ForwardSource(chat_id=-100777, message_ids=(5, 6)))
        outcome = run(transport(bot).publish(content))
        assert outcome == UncertainOutcome(code="partial_result", confirmed_message_ids=(30,))


def api_method():
    return SendMessage(chat_id=1, text="x")


class TestConfiguredUsernameTarget:
    """A @username config arrives already RESOLVED: sends target the numeric
    id; the username only upgrades permalinks to the public t.me form."""

    def transport(self, bot):
        return AiogramChannelTransport(
            bot, channel_id=CHANNEL, owner_id=OWNER,
            channel_username="@test_chan_public2",
        )

    def test_single_send_targets_numeric_id_with_username_permalink(self):
        bot = StubBot(result=message(7))  # chat payload carries no username
        outcome = run(self.transport(bot).publish(text_content("hello")))
        assert outcome == Sent(
            message_ids=(7,), permalink="https://t.me/test_chan_public2/7"
        )
        _, kwargs = bot.calls[0]
        assert kwargs["chat_id"] == CHANNEL  # never the username string

    def test_delivered_chat_username_wins_over_configured(self):
        bot = StubBot(result=message(7, username="actual_name"))
        outcome = run(self.transport(bot).publish(text_content("hello")))
        assert outcome.permalink == "https://t.me/actual_name/7"

    def test_album_uses_configured_username_fallback(self):
        bot = StubBot(result=[message(20), message(21)])
        content = Content(
            kind="album",
            items=(MediaRef("photo", "p1"), MediaRef("video", "v1")),
            caption="album text",
        )
        outcome = run(self.transport(bot).publish(content))
        assert outcome.permalink == "https://t.me/test_chan_public2/21"

    def test_forward_without_chat_payload_uses_configured_username(self):
        bot = StubBot(result=[message_id(30), message_id(31)])
        content = Content(kind="forward", mode="forward",
                          forward=ForwardSource(chat_id=-100777, message_ids=(5, 6)))
        outcome = run(self.transport(bot).publish(content))
        assert outcome.permalink == "https://t.me/test_chan_public2/31"

    def test_group_message_chat_username_produces_public_link(self):
        bot = StubBot(result=[
            message(40, username="public_chan"), message(41, username="public_chan"),
        ])
        content = Content(
            kind="album",
            items=(MediaRef("photo", "p1"), MediaRef("video", "v1")),
        )
        outcome = run(self.transport(bot).publish(content))
        assert outcome.permalink == "https://t.me/public_chan/41"

    def test_preview_permalink_stays_empty_for_owner_copy(self):
        bot = StubBot(result=[message_id(99)])
        outcome = run(self.transport(bot).preview_sent(7))
        assert isinstance(outcome, Sent) and outcome.permalink == ""

    def test_bare_and_at_prefixed_usernames_are_equivalent(self):
        bot = StubBot(result=message(7))
        bare = AiogramChannelTransport(
            bot, channel_id=CHANNEL, owner_id=OWNER, channel_username="name_one"
        )
        prefixed = AiogramChannelTransport(
            bot, channel_id=CHANNEL, owner_id=OWNER, channel_username="@name_one"
        )
        assert run(bare.publish(text_content("x"))).permalink == \
            run(prefixed.publish(text_content("x"))).permalink == \
            "https://t.me/name_one/7"

    @pytest.mark.parametrize("bad_target", ["@chan_one", "chan_one", 0, None, True])
    def test_constructor_refuses_unresolved_or_placeholder_targets(self, bad_target):
        with pytest.raises(ValueError):
            AiogramChannelTransport(StubBot(), channel_id=bad_target, owner_id=OWNER)

    def test_build_transport_refuses_unresolved_username_config(self):
        from poster_bot.config import (
            ENV_CHANNEL,
            ENV_OWNER,
            ENV_TOKEN,
            ConfigError,
            load_config,
        )

        config = load_config(
            {ENV_TOKEN: "123:fake-offline", ENV_OWNER: str(OWNER),
             ENV_CHANNEL: "@test_chan_public2"}
        )
        with pytest.raises(ConfigError) as caught:
            build_transport(config, bot=StubBot())
        assert ENV_CHANNEL in _all(caught.value)
        assert "@test_chan_public2" not in _all(caught.value)


def _all(exc) -> str:
    return " | ".join(exc.problems)


class TestErrorClassification:

    @pytest.mark.parametrize("exc", [
        asyncio.TimeoutError(),
        TelegramNetworkError(method=api_method(), message="connection reset"),
        TelegramServerError(method=api_method(), message="502 bad gateway"),
        OSError("host unreachable"),
    ])
    def test_ambiguous_failures_park_uncertain(self, exc):
        bot = StubBot(raises=exc)
        outcome = run(transport(bot).publish(text_content()))
        assert isinstance(outcome, UncertainOutcome)
        assert outcome.code in {"timeout", "transport_error"}

    def test_timeout_error_code_is_distinct(self):
        bot = StubBot(raises=asyncio.TimeoutError())
        assert run(transport(bot).publish(text_content())) == \
            UncertainOutcome(code="timeout")

    @pytest.mark.parametrize("exc", [
        TelegramBadRequest(method=api_method(), message="400: entity too large"),
    ])
    def test_rejections_are_definitive(self, exc):
        bot = StubBot(raises=exc)
        assert run(transport(bot).publish(text_content())) == \
            DefinitiveFailure(code="api_error")

    def test_rate_limit_is_definitive_no_auto_retry(self):
        bot = StubBot(raises=TelegramRetryAfter(
            method=api_method(), message="429: too many", retry_after=30
        ))
        assert run(transport(bot).publish(text_content())) == \
            DefinitiveFailure(code="rate_limited")

    def test_exception_text_never_leaves_the_adapter(self):
        secret = "BOT-TOKEN-SECRET 401 Unauthorized"
        bot = StubBot(raises=TelegramNetworkError(method=api_method(), message=secret))
        outcome = run(transport(bot).publish(text_content()))
        assert secret not in repr(outcome)  # only allowlisted codes travel


class TestPreviewAndNotice:
    def test_preview_copies_to_owner_only(self):
        bot = StubBot(result=[message_id(99)])
        outcome = run(transport(bot).preview_sent(7))
        assert isinstance(outcome, Sent)
        _, kwargs = bot.calls[0]
        assert kwargs["chat_id"] == OWNER and kwargs["from_chat_id"] == CHANNEL

    def test_empty_preview_copy_is_partial(self):
        bot = StubBot(result=[])
        assert run(transport(bot).preview_sent(7)) == \
            UncertainOutcome(code="partial_result")

    def test_notice_goes_to_owner(self):
        bot = StubBot(result=message(1))
        run(transport(bot).notify_owner("bot recovered"))
        _, kwargs = bot.calls[0]
        assert kwargs["chat_id"] == OWNER and kwargs["text"] == "bot recovered"

    def test_notice_failure_raises_allowlisted_code_only(self):
        bot = StubBot(raises=TelegramBadRequest(
            method=api_method(), message="400: bot blocked"
        ))
        with pytest.raises(TransportCallError) as err:
            run(transport(bot).notify_owner("hi"))
        assert err.value.code == "api_error"
        assert "blocked" not in str(err.value)


class TestEntityMapping:
    def test_utf16_offsets_pass_through_unchanged(self):
        # astral-plane chars: entity spans two of them (4 UTF-16 units)
        entities = to_aiogram_entities((
            Entity(type="bold", offset=0, length=4),
            Entity(type="custom_emoji", offset=5, length=2,
                   custom_emoji_id="emoji1"),
        ))
        assert entities[0].offset == 0 and entities[0].length == 4
        assert entities[1].custom_emoji_id == "emoji1"

    def test_language_and_url_survive(self):
        entities = to_aiogram_entities((
            Entity(type="code", offset=0, length=3, language="ru"),
            Entity(type="url", offset=4, length=10, url="https://t.me/x"),
        ))
        assert entities[0].language == "ru"
        assert entities[1].url == "https://t.me/x"


class TestBotComposition:
    def test_created_bot_has_bounded_timeout_and_no_retries(self):
        from poster_bot.config import Config

        config = Config(
            bot_token="123456:ABCDEF", owner_user_id=OWNER, channel_id=CHANNEL,
            db_path=":memory:", lock_path="/tmp/none", timezone="UTC",
            slot_start_hour=0, slot_end_hour=24,
        )
        bot = create_bot(config)
        assert bot.session.timeout == SENDER_TIMEOUT_SECONDS
        # aiogram ships no retry middleware and none is registered: exactly
        # one HTTP request per call, so no call can duplicate a channel post.
        assert bot.session.middleware._middlewares == []


class TestBuildTransportRealConfig:
    """build_transport must wire the REAL Config helper, not imagined fields."""

    def _config(self):
        from poster_bot.config import (
            ENV_CHANNEL,
            ENV_DB_PATH,
            ENV_END,
            ENV_LOCK_PATH,
            ENV_OWNER,
            ENV_START,
            ENV_TZ,
            ENV_TOKEN,
            load_config,
        )

        return load_config(
            {  # fake env only: the real .env is never read
                ENV_TOKEN: "123:fake-offline-token-value",
                ENV_OWNER: str(OWNER),
                ENV_CHANNEL: str(CHANNEL),
                ENV_DB_PATH: "unused-under-test.sqlite3",
                ENV_LOCK_PATH: "unused-under-test.lock",
                ENV_TZ: "UTC",
                ENV_START: "0",
                ENV_END: "23",
            }
        )

    def test_build_transport_wires_owner_user_id_from_loaded_config(self):
        # regression: this once read config.owner_id, which does not exist
        config = self._config()
        bot = StubBot(result=message(1))
        channel_transport = build_transport(config, bot=bot)
        run(channel_transport.notify_owner("ping"))
        _, kwargs = bot.calls[0]
        assert kwargs["chat_id"] == OWNER  # config.owner_user_id reached the bot

    def test_build_transport_binds_channel_for_publish(self):
        config = self._config()
        bot = StubBot(result=message(2))
        channel_transport = build_transport(config, bot=bot)
        run(channel_transport.publish(text_content("post")))
        _, kwargs = bot.calls[0]
        assert kwargs["chat_id"] == CHANNEL


def edit_request(**over) -> NavEditRequest:
    base = dict(message_id=9002, text="часть 2", entities=(), kind="text")
    base.update(over)
    return NavEditRequest(**base)


class TestNavigationEdits:
    """ChannelEditor half of the adapter: post-publish navigation edits are
    bound to the channel, reuse the frozen classification, never raise."""

    def test_text_edit_targets_channel_never_owner(self):
        bot = StubBot(result=message(9002))
        outcome = run(transport(bot).edit_published_part(edit_request()))
        assert outcome == Sent(message_ids=(9002,),
                               permalink="https://t.me/c/1234567890/9002")
        name, kwargs = bot.calls[0]
        assert name == "edit_message_text"
        assert kwargs["chat_id"] == CHANNEL
        assert kwargs["text"] == "часть 2"
        assert [k["chat_id"] for _n, k in bot.calls] == [CHANNEL]

    def test_entities_convert_for_edit(self):
        from poster_bot.domain.content import Entity

        bot = StubBot(result=message(9002))
        request = edit_request(entities=(
            Entity(type="bold", offset=0, length=5),
            Entity(type="text_link", offset=7, length=4,
                   url="https://t.me/c/1/9"),
        ))
        run(transport(bot).edit_published_part(request))
        _, kwargs = bot.calls[0]
        assert [e.type for e in kwargs["entities"]] == ["bold", "text_link"]
        assert kwargs["entities"][1].url == "https://t.me/c/1/9"

    def test_caption_edit_uses_caption_api(self):
        bot = StubBot(result=message(9002))
        outcome = run(transport(bot).edit_published_part(
            edit_request(kind="caption", text="подпись")))
        assert isinstance(outcome, Sent)
        name, kwargs = bot.calls[0]
        assert name == "edit_message_caption"
        assert kwargs["chat_id"] == CHANNEL
        assert kwargs["caption"] == "подпись"
        assert "text" not in kwargs

    def test_api_error_is_definitive_never_raises(self):
        from aiogram.exceptions import TelegramBadRequest

        bot = StubBot(raises=TelegramBadRequest(method=api_method(),
                                                message="message can't be edited"))
        outcome = run(transport(bot).edit_published_part(edit_request()))
        assert outcome == DefinitiveFailure(code="api_error")

    def test_timeout_is_uncertain(self):
        bot = StubBot(raises=TimeoutError())
        outcome = run(transport(bot).edit_published_part(edit_request()))
        assert outcome == UncertainOutcome(code="timeout")

    def test_network_error_is_uncertain(self):
        from aiogram.exceptions import TelegramNetworkError

        bot = StubBot(raises=TelegramNetworkError(method=api_method(),
                                                  message="connection reset"))
        outcome = run(transport(bot).edit_published_part(edit_request()))
        assert outcome == UncertainOutcome(code="transport_error")

    def test_unexpected_error_parks_uncertain(self):
        class Boom(Exception):
            pass

        bot = StubBot(raises=Boom())
        outcome = run(transport(bot).edit_published_part(edit_request()))
        assert outcome == UncertainOutcome(code="transport_error")

    def test_edit_never_touches_owner_chat(self):
        bot = StubBot(result=message(9002))
        run(transport(bot).edit_published_part(edit_request(kind="caption")))
        assert [k["chat_id"] for _n, k in bot.calls] == [CHANNEL]
