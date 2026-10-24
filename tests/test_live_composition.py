"""Live composition (poster_bot.live) exercised OFFLINE.

A real aiogram Bot over the existing offline fake API session (canned
responses, zero sockets) plus a fake clock: the wiring itself — startup
order, malformed-token refusal with lock release, durable poll cycles,
owner-router dispatch of canned updates, failure classification, clean
SIGTERM shutdown — is production code throughout. The fake session never
talks to the channel chat: every recorded call proves it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import warnings
from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetChat, GetUpdates
from aiogram.types import ReplyKeyboardMarkup
from conftest import NOW, text_content
from test_ui_handlers_offline import _FakeApiSession

from poster_bot.config import (
    ENV_CHANNEL,
    ENV_SIG_MAIN_TEXT,
    ENV_SIG_MAIN_URL,
    ENV_SIG_PARTNER_TEXT,
    ENV_SIG_PARTNER_URL,
    ENV_TOKEN,
    ChannelResolutionError,
    Config,
    ConfigError,
    MalformedTokenError,
)
from poster_bot.live import PollFatal, compose_live, run_live
from poster_bot.polling import POLL_TIMEOUT_SECONDS
from poster_bot.singleton import ProcessLock
from poster_bot.transport.ports import Sent
from poster_bot.ui import texts
from poster_bot.ui.navigation import NavigationStore
from planned_helpers import SIG

OWNER = 111
CHANNEL = -100222
GOOD_TOKEN = "123:abcdefghij"
BAD_TOKEN = "BADTOKENnocolon"
_DATE = "2026-10-21T08:00:00+00:00"


def live_config(tmp_path, **over) -> Config:
    base = dict(
        bot_token=GOOD_TOKEN,
        owner_user_id=OWNER,
        channel_id=CHANNEL,
        db_path=str(tmp_path / "bot.sqlite3"),
        lock_path=str(tmp_path / "bot.lock"),
        timezone="Europe/Moscow",
        slot_start_hour=0,
        slot_end_hour=23,
        signature=SIG,
    )
    base.update(over)
    return Config(**base)


class LiveApiSession(_FakeApiSession):
    """Adds scripted getUpdates polling and close() recording on top of the
    offline UI-test session. Any attempt to fetch files still hard-fails."""

    def __init__(self) -> None:
        super().__init__()
        self.update_script: list[Any] = []
        self.poll_calls: list[dict] = []
        self.on_poll = None
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def make_request(self, bot, method, timeout=None):
        if method.__api_method__ == "getUpdates":
            self.poll_calls.append(
                {"offset": method.offset, "timeout": method.timeout,
                 "limit": method.limit, "allowed_updates": method.allowed_updates}
            )
            if self.on_poll is not None:
                self.on_poll()
            item = self.update_script.pop(0) if self.update_script else []
            if isinstance(item, Exception):
                raise item
            raw = item  # list of raw update payloads (empty = quiet poll)
            assert isinstance(raw, list) and all(
                isinstance(entry, dict) for entry in raw
            ), "getUpdates script entry must be an Exception or a list of dicts"
            content = json.dumps({"ok": True, "result": raw})
            response = self.check_response(
                bot=bot, method=method, status_code=200, content=content
            )
            return response.result
        return await super().make_request(bot, method, timeout)

    def sent_chats(self) -> list[int]:
        return [p["chat_id"] for name, p in self.requests if name == "sendMessage"]

    def sent_to_channel(self) -> list[dict]:
        return [p for n, p in self.requests
                if n == "sendMessage" and p["chat_id"] == CHANNEL]


def make_bot(session: LiveApiSession) -> Bot:
    return Bot(GOOD_TOKEN, session=session)


class ResolvingApiSession(LiveApiSession):
    """Adds scripted getChat responses for channel-target resolution: canned
    payloads through aiogram's real validation path, zero sockets."""

    def __init__(self) -> None:
        super().__init__()
        self.chat_payload: Any = None
        self.chat_error: Exception | None = None
        self.chat_calls: list[dict] = []

    async def make_request(self, bot, method, timeout=None):
        if method.__api_method__ == "getChat":
            self.chat_calls.append({"chat_id": method.chat_id})
            if self.chat_error is not None:
                raise self.chat_error
            assert isinstance(self.chat_payload, dict), "getChat script missing"
            content = json.dumps({"ok": True, "result": self.chat_payload})
            response = self.check_response(
                bot=bot, method=method, status_code=200, content=content
            )
            return response.result
        return await super().make_request(bot, method, timeout)


def start_update(update_id: int, *, chat_id: int = OWNER, user_id: int = OWNER):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "date": _DATE,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Owner"},
            "text": "/start",
        },
    }


def lock_free(config: Config) -> bool:
    lock = ProcessLock(config.lock_path)
    lock.acquire()
    lock.release()
    return True


class TestTokenGate:
    def test_malformed_token_refused_by_name_only_and_lock_released(self, tmp_path):
        config = live_config(tmp_path, bot_token=BAD_TOKEN)
        session = LiveApiSession()
        with pytest.raises(MalformedTokenError) as caught:
            asyncio.run(compose_live(config, clock=lambda: NOW, bot=make_bot(session),
                         now_epoch=NOW))
        message = str(caught.value)
        assert ENV_TOKEN in message          # names the env var...
        assert BAD_TOKEN not in message      # ...never the token value
        assert session.poll_calls == []      # no poll was ever attempted
        assert lock_free(config)             # lock/DB released
        # second attempt: refused for the SAME reason, not by the lock
        with pytest.raises(MalformedTokenError):
            asyncio.run(compose_live(config, clock=lambda: NOW, bot=make_bot(session),
                         now_epoch=NOW))

    def test_live_gate_blocks_before_token_syntax(self, tmp_path):
        # owner/channel still unset: the durable gate refuses first; the
        # malformed token is never even looked at
        config = live_config(tmp_path, bot_token=BAD_TOKEN, owner_user_id=0,
                             channel_id=0)
        with pytest.raises(ConfigError) as caught:
            asyncio.run(compose_live(config, clock=lambda: NOW,
                         bot=make_bot(LiveApiSession()), now_epoch=NOW))
        assert not isinstance(caught.value, MalformedTokenError)
        assert any("POSTER_BOT_OWNER_USER_ID" in p for p in caught.value.problems)

    def test_empty_token_blocked_by_live_gate(self, tmp_path):
        config = live_config(tmp_path, bot_token="")
        with pytest.raises(ConfigError) as caught:
            asyncio.run(compose_live(config, clock=lambda: NOW,
                         bot=make_bot(LiveApiSession()), now_epoch=NOW))
        assert any(ENV_TOKEN in p for p in caught.value.problems)
        assert "placeholder" in str(caught.value) or "missing" in str(caught.value)


class TestCompositionWiring:
    def test_cycle_polls_durable_offset_and_records_health(self, tmp_path):
        config = live_config(tmp_path)
        session = LiveApiSession()
        live = asyncio.run(compose_live(config, clock=lambda: NOW, bot=make_bot(session),
                            now_epoch=NOW))
        try:
            assert live.dispatcher.sub_routers[0].name == "owner_ui"
            report = asyncio.run(live.loop.run_cycle())
            assert report.ok
            call = session.poll_calls[-1]
            assert call["offset"] is None
            assert call["timeout"] == POLL_TIMEOUT_SECONDS
            assert call["allowed_updates"] == ["message", "callback_query"]
            assert live.app.runtime.last_poll() == NOW
            assert live.inbox.safe_offset() is None
            assert session.sent_to_channel() == []  # no network, no channel sends
            # lock is held while live is up
            with pytest.raises(Exception):
                ProcessLock(config.lock_path).acquire()
        finally:
            asyncio.run(live.close())
        assert session.closed
        assert lock_free(config)

    def test_owner_update_flows_through_composed_router(self, tmp_path):
        config = live_config(tmp_path)
        session = LiveApiSession()
        session.update_script = [[start_update(501)]]
        live = asyncio.run(compose_live(config, clock=lambda: NOW, bot=make_bot(session),
                            now_epoch=NOW))
        try:
            report = asyncio.run(live.loop.run_cycle())
            assert report.ok and report.handled == 1
            assert live.inbox.is_done(501) and live.inbox.safe_offset() == 502
            # the owner got a menu reply — delivered via the composed flow
            assert OWNER in session.sent_chats()
            assert session.sent_to_channel() == []
        finally:
            asyncio.run(live.close())

    def test_nonowner_update_gets_no_reply_but_is_completed(self, tmp_path):
        config = live_config(tmp_path)
        session = LiveApiSession()
        session.update_script = [[start_update(601, chat_id=999, user_id=999)]]
        live = asyncio.run(compose_live(config, clock=lambda: NOW, bot=make_bot(session),
                            now_epoch=NOW))
        try:
            report = asyncio.run(live.loop.run_cycle())
            assert report.handled == 1  # guard drops, but the update is done
            assert live.inbox.is_done(601)
            assert session.sent_chats() == []  # unauthorized: silence
        finally:
            asyncio.run(live.close())

    def test_aiogram_errors_map_to_safe_poll_codes(self, tmp_path):
        config = live_config(tmp_path)
        session = LiveApiSession()
        session.update_script = [TelegramNetworkError(method=GetUpdates(),
                                                      message="secret text")]
        live = asyncio.run(compose_live(config, clock=lambda: NOW, bot=make_bot(session),
                            now_epoch=NOW))
        try:
            report = asyncio.run(live.loop.run_cycle())
            assert not report.ok and report.failure_code == "transport_error"
            assert "secret" not in str(report)  # telegram text never surfaces
            session.update_script = [TelegramUnauthorizedError(
                method=GetUpdates(), message="Unauthorized")]
            with pytest.raises(PollFatal) as caught:
                asyncio.run(live.loop.run_cycle())
            assert caught.value.code == "unauthorized"
        finally:
            asyncio.run(live.close())


class TestRunLive:
    def _run(self, config, session, **kw):
        kw.setdefault("stop_event", asyncio.Event())
        return asyncio.run(run_live(config, clock=lambda: NOW,
                                    bot=make_bot(session), **kw))

    def test_pre_set_stop_exits_cleanly(self, tmp_path):
        config = live_config(tmp_path)
        session = LiveApiSession()
        stop = asyncio.Event()
        stop.set()
        started: list[tuple[int, ...]] = []
        self._run(config, session, stop_event=stop, install_signals=False,
                  now_epoch=NOW, on_started=lambda ids: started.append(ids))
        assert started == [()]  # loop never ran: no notices queued at open
        assert session.poll_calls == []
        assert session.closed
        assert lock_free(config)
        # poller_stopped journaled on clean exit
        import sqlite3

        db = sqlite3.connect(config.db_path)
        rows = {r[0] for r in db.execute("SELECT kind FROM journal_events")}
        db.close()
        assert "poller_stopped" in rows

    def test_poll_then_stop_event_stops_promptly(self, tmp_path):
        config = live_config(tmp_path)
        session = LiveApiSession()
        stop = asyncio.Event()

        def stop_after_two_polls():
            if len(session.poll_calls) >= 2:
                stop.set()

        session.on_poll = stop_after_two_polls
        self._run(config, session, stop_event=stop, install_signals=False,
                  now_epoch=NOW)
        assert len(session.poll_calls) == 2
        assert session.closed and lock_free(config)

    def test_sigterm_stops_gracefully(self, tmp_path):
        config = live_config(tmp_path)
        session = LiveApiSession()
        fired = {"done": False}

        def fire_sigterm():
            if not fired["done"]:
                fired["done"] = True
                os.kill(os.getpid(), signal.SIGTERM)

        session.on_poll = fire_sigterm
        self._run(config, session, install_signals=True, now_epoch=NOW)
        assert fired["done"]
        # the signal must take effect within a cycle or two (the delivery's
        # loop wakeup can race one resume); a lost signal would spin forever
        assert 1 <= len(session.poll_calls) <= 5
        assert session.closed and lock_free(config)

    def test_fatal_poll_cleans_up_then_propagates(self, tmp_path):
        config = live_config(tmp_path)
        session = LiveApiSession()
        session.update_script = [TelegramUnauthorizedError(
            method=GetUpdates(), message="Unauthorized")]
        with pytest.raises(PollFatal):
            self._run(config, session, install_signals=False, now_epoch=NOW)
        assert session.closed and lock_free(config)


class ExplodingApiSession(LiveApiSession):
    """Fails every sendMessage with an error message embedding private post
    text AND the token: simulates a leaky Telegram error description so any
    composition path that echoes exception text into logs is caught."""

    leak: str = ""

    async def make_request(self, bot, method, timeout=None):
        if method.__api_method__ == "sendMessage":
            raise TelegramAPIError(method=method, message=self.leak)
        return await super().make_request(bot, method, timeout)


class TestInboundFailureLogHygiene:
    """Fake inbound failures must never log owner content or the token —
    captured at DEBUG so even stray debug records from aiogram are checked."""

    SENTINEL = "PRIVATE-OWNER-DRAFT-TEXT-4c1a"

    def _owner_text_update(self, update_id: int):
        return {
            "update_id": update_id,
            "message": {
                "message_id": 7,
                "date": _DATE,
                "chat": {"id": OWNER, "type": "private"},
                "from": {"id": OWNER, "is_bot": False, "first_name": "Owner"},
                "text": f"save this draft {self.SENTINEL}",
            },
        }

    def test_handler_exception_never_logs_content_or_token(self, tmp_path, caplog):
        config = live_config(tmp_path)
        session = ExplodingApiSession()
        session.leak = (
            f"400 Bad Request: cannot parse '{self.SENTINEL}'"
            f" (token {GOOD_TOKEN})"
        )
        session.update_script = [[self._owner_text_update(801)]]
        live = asyncio.run(compose_live(config, clock=lambda: NOW, bot=make_bot(session),
                            now_epoch=NOW))
        try:
            with caplog.at_level(logging.DEBUG):
                report = asyncio.run(live.loop.run_cycle())
            assert report.ok
            assert report.handler_failures == 1 and report.handled == 0
            # the row stays pending for re-delivery (at-least-once reality)
            assert not live.inbox.is_done(801)
            assert session.sent_to_channel() == []
            assert self.SENTINEL not in caplog.text
            assert GOOD_TOKEN not in caplog.text
            assert all(getattr(r, "exc_info", None) is None
                       for r in caplog.records)
            assert "TelegramAPIError" in caplog.text  # class name only
        finally:
            asyncio.run(live.close())

    def test_untypable_update_skipped_without_content_dump(self, tmp_path, caplog):
        # aiogram keeps unknown payload fields (extra="allow") and its
        # _listen_update warn-dumps the full update JSON: the composed
        # handler must settle such updates before aiogram sees them.
        config = live_config(tmp_path)
        session = LiveApiSession()
        session.update_script = [[{
            "update_id": 902,
            "quantum_signal": {"text": f"{self.SENTINEL} via {GOOD_TOKEN}"},
        }]]
        live = asyncio.run(compose_live(config, clock=lambda: NOW, bot=make_bot(session),
                            now_epoch=NOW))
        try:
            with caplog.at_level(logging.DEBUG), \
                    warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                report = asyncio.run(live.loop.run_cycle())
            assert report.ok and report.handled == 1
            assert live.inbox.is_done(902)  # no handler work owed: completes
            assert session.sent_chats() == []  # nothing was ever answered
            # the aiogram warn-dump was never triggered at all
            assert [str(w.message) for w in caught] == []
            assert self.SENTINEL not in caplog.text
            assert GOOD_TOKEN not in caplog.text
            assert any("902" in r.getMessage() and "quantum" not in r.getMessage()
                       for r in caplog.records)  # safe skip line names the id
        finally:
            asyncio.run(live.close())

    def test_malformed_token_error_carries_no_value_in_any_channel(
        self, tmp_path, caplog
    ):
        config = live_config(tmp_path, bot_token=BAD_TOKEN)
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(MalformedTokenError) as caught:
                asyncio.run(compose_live(config, clock=lambda: NOW,
                             bot=make_bot(LiveApiSession()), now_epoch=NOW))
        assert caught.value.__cause__ is None  # aiogram error fully suppressed
        assert BAD_TOKEN not in str(caught.value)
        assert BAD_TOKEN not in caplog.text


USERNAME = "@test_chan_public2"
RESOLVED = -1009876543  # stable numeric id the fake returns for USERNAME


def channel_payload(**over) -> dict:
    # getChat returns ChatFullInfo (aiogram 3.31): the full-info fields are
    # required by the response model — canned payload only, no real data
    raw = {
        "id": RESOLVED,
        "type": "channel",
        "username": "test_chan_public2",
        "title": "Test Channel",
        "accent_color_id": 1,
        "max_reaction_count": 10,
        "accepted_gift_types": {
            "unlimited_gifts": 0, "limited_gifts": 0, "unique_gifts": 0,
            "premium_subscription": 0, "gifts_from_channels": 0,
        },
    }
    raw.update(over)
    return raw


class TestChannelTargetResolution:
    """Live startup resolves a configured @username via getChat BEFORE any
    transport/flow exists; every send targets the returned numeric id; a
    refused resolution releases lock/DB/session with zero send attempts."""

    def _live(self, config, session):
        return asyncio.run(compose_live(config, clock=lambda: NOW,
                                        bot=make_bot(session), now_epoch=NOW))

    def test_numeric_target_performs_no_get_chat(self, tmp_path):
        # getChat script intentionally missing: any resolution attempt for a
        # numeric target would hard-fail this composition
        config = live_config(tmp_path)  # channel_id = -100222 (numeric)
        session = ResolvingApiSession()
        live = self._live(config, session)
        try:
            assert session.chat_calls == []
            assert "getChat" not in [name for name, _ in session.requests]
        finally:
            asyncio.run(live.close())

    def test_username_resolves_to_numeric_destination_used_by_sends(self, tmp_path):
        config = live_config(tmp_path, channel_id=USERNAME)
        session = ResolvingApiSession()
        session.chat_payload = channel_payload()
        live = self._live(config, session)
        try:
            assert session.chat_calls == [{"chat_id": USERNAME}]
            outcome = asyncio.run(live.transport.publish(text_content("hello")))
            assert isinstance(outcome, Sent)
            sends = [p for name, p in session.requests if name == "sendMessage"]
            assert sends and sends[-1]["chat_id"] == RESOLVED  # numeric, not @name
            assert outcome.permalink == (
                f"https://t.me/test_chan_public2/{outcome.message_ids[0]}"
            )
        finally:
            asyncio.run(live.close())

    def test_owner_ui_paths_stay_private_under_resolved_username(self, tmp_path):
        config = live_config(tmp_path, channel_id=USERNAME)
        session = ResolvingApiSession()
        session.chat_payload = channel_payload()
        session.update_script = [[start_update(511)]]
        live = self._live(config, session)
        try:
            report = asyncio.run(live.loop.run_cycle())
            assert report.ok and report.handled == 1
            # Telegram needs separate messages for the persistent input menu
            # and service card. Both must remain private after resolution.
            assert session.sent_chats() == [OWNER, OWNER]
            sends = [p for name, p in session.requests if name == "sendMessage"]
            keyboard_messages = [p for p in sends
                                 if "keyboard" in p.get("reply_markup", {})]
            assert len(keyboard_messages) == 1
            keyboard = ReplyKeyboardMarkup.model_validate(
                keyboard_messages[0]["reply_markup"]
            )
            assert keyboard.resize_keyboard is True
            assert keyboard.is_persistent is True
            assert [[b.text for b in row] for row in keyboard.keyboard] == [
                [texts.MENU_NEW_POST, texts.MENU_DRAFTS],
                [texts.MENU_PLAN, texts.MENU_SETTINGS],
                [texts.MENU_JOURNAL, texts.MENU_RESTART],
            ]
            assert texts.MENU_JOURNAL in [
                b.text for row in keyboard.keyboard for b in row
            ]
            card_id = NavigationStore(live.app.conn, OWNER).get(OWNER)
            assert card_id is not None
            card = session.visible[card_id]
            assert card in sends and card != keyboard_messages[0]
            assert card["text"] == f"{texts.PLAN_DAY_PREFIX}21.10.2026 (Europe/Moscow): {texts.NO_PLANS_DAY}"
            assert [[b["text"] for b in row] for row in
                    card["reply_markup"]["inline_keyboard"]] == [
                        ["← Пред.", "Сегодня", "След. →"], ["Создать пост"]]
            # Check all chat-addressed operations, not just text sends: neither
            # the numeric channel nor its username may receive UI output.
            assert all(p["chat_id"] == OWNER for _, p in session.requests
                       if "chat_id" in p)
        finally:
            asyncio.run(live.close())

    def test_group_destination_refused_before_any_send(self, tmp_path):
        config = live_config(tmp_path, channel_id=USERNAME)
        session = ResolvingApiSession()
        session.chat_payload = channel_payload(
            id=-100444, type="group", username=None)
        with pytest.raises(ChannelResolutionError) as caught:
            self._live(config, session)
        message = str(caught.value)
        assert ENV_CHANNEL in message and "not a channel" in message
        assert USERNAME not in message  # values are never echoed
        assert not [n for n, _ in session.requests
                    if n in {"sendMessage", "sendPhoto", "sendVideo",
                             "sendMediaGroup", "forwardMessages"}]
        assert session.closed and lock_free(config)

    def test_channel_type_with_positive_id_refused(self, tmp_path):
        config = live_config(tmp_path, channel_id=USERNAME)
        session = ResolvingApiSession()
        session.chat_payload = channel_payload(id=555)
        with pytest.raises(ChannelResolutionError):
            self._live(config, session)
        assert session.closed and lock_free(config)

    def test_get_chat_api_failure_maps_to_safe_code_no_leak(self, tmp_path):
        leak = "400: chat not found mentioning PRIVATE-TEXT-9f31"
        config = live_config(tmp_path, channel_id=USERNAME)
        session = ResolvingApiSession()
        session.chat_error = TelegramBadRequest(
            method=GetChat(chat_id=USERNAME), message=leak)
        with pytest.raises(ChannelResolutionError) as caught:
            self._live(config, session)
        message = str(caught.value)
        assert "not accessible" in message
        assert leak not in message and USERNAME not in message
        assert not [n for n, _ in session.requests if n == "sendMessage"]
        assert session.closed and lock_free(config)

    def test_network_failure_maps_to_unreachable(self, tmp_path):
        config = live_config(tmp_path, channel_id=USERNAME)
        session = ResolvingApiSession()
        session.chat_error = TelegramNetworkError(
            method=GetChat(chat_id=USERNAME), message="connection reset")
        with pytest.raises(ChannelResolutionError) as caught:
            self._live(config, session)
        assert "Telegram not reachable" in str(caught.value)
        assert session.closed and lock_free(config)

    def test_malformed_token_still_refused_before_resolution(self, tmp_path):
        # startup order intact: token syntax precedes any getChat
        config = live_config(tmp_path, bot_token=BAD_TOKEN, channel_id=USERNAME)
        session = ResolvingApiSession()
        with pytest.raises(MalformedTokenError):
            self._live(config, session)
        assert session.chat_calls == []
        assert lock_free(config)

    def test_run_live_resolves_username_then_polls_and_cleans_up(self, tmp_path):
        config = live_config(tmp_path, channel_id=USERNAME)
        session = ResolvingApiSession()
        session.chat_payload = channel_payload()
        stop = asyncio.Event()

        def stop_after_one_poll():
            if len(session.poll_calls) >= 1:
                stop.set()

        session.on_poll = stop_after_one_poll
        asyncio.run(run_live(config, clock=lambda: NOW, bot=make_bot(session),
                             stop_event=stop, install_signals=False,
                             now_epoch=NOW))
        assert session.chat_calls == [{"chat_id": USERNAME}]
        assert len(session.poll_calls) == 1
        assert not [n for n, _ in session.requests if n == "sendMessage"]
        assert session.closed and lock_free(config)


class TestSignatureGate:
    """Live startup refuses to compose a sender without the mandatory
    signature (unsigned publishes would otherwise reach the channel)."""

    def test_missing_signature_refused_before_any_network_call(self, tmp_path):
        config = live_config(tmp_path, channel_id="@chan", signature=None)
        session = ResolvingApiSession()
        with pytest.raises(ConfigError) as exc:
            asyncio.run(compose_live(config, clock=lambda: NOW,
                                     bot=make_bot(session), now_epoch=NOW))
        message = str(exc.value)
        for name in (ENV_SIG_MAIN_TEXT, ENV_SIG_MAIN_URL,
                     ENV_SIG_PARTNER_TEXT, ENV_SIG_PARTNER_URL):
            assert name in message
        # strictly before create_bot/resolve_channel: no session use at all
        assert session.requests == []
        assert session.chat_calls == []
        assert lock_free(config)

    def test_token_check_precedes_signature_gate(self, tmp_path):
        config = live_config(tmp_path, bot_token=BAD_TOKEN, signature=None)
        session = LiveApiSession()
        with pytest.raises(MalformedTokenError):
            asyncio.run(compose_live(config, clock=lambda: NOW,
                                     bot=make_bot(session), now_epoch=NOW))
        assert session.requests == []
        assert lock_free(config)
