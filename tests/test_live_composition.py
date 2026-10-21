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
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetUpdates
from conftest import NOW
from test_ui_handlers_offline import _FakeApiSession

from poster_bot.config import (
    ENV_TOKEN,
    Config,
    ConfigError,
    MalformedTokenError,
)
from poster_bot.live import PollFatal, compose_live, run_live
from poster_bot.polling import POLL_TIMEOUT_SECONDS
from poster_bot.singleton import ProcessLock

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
            compose_live(config, clock=lambda: NOW, bot=make_bot(session),
                         now_epoch=NOW)
        message = str(caught.value)
        assert ENV_TOKEN in message          # names the env var...
        assert BAD_TOKEN not in message      # ...never the token value
        assert session.poll_calls == []      # no poll was ever attempted
        assert lock_free(config)             # lock/DB released
        # second attempt: refused for the SAME reason, not by the lock
        with pytest.raises(MalformedTokenError):
            compose_live(config, clock=lambda: NOW, bot=make_bot(session),
                         now_epoch=NOW)

    def test_live_gate_blocks_before_token_syntax(self, tmp_path):
        # owner/channel still unset: the durable gate refuses first; the
        # malformed token is never even looked at
        config = live_config(tmp_path, bot_token=BAD_TOKEN, owner_user_id=0,
                             channel_id=0)
        with pytest.raises(ConfigError) as caught:
            compose_live(config, clock=lambda: NOW,
                         bot=make_bot(LiveApiSession()), now_epoch=NOW)
        assert not isinstance(caught.value, MalformedTokenError)
        assert any("POSTER_BOT_OWNER_USER_ID" in p for p in caught.value.problems)

    def test_empty_token_blocked_by_live_gate(self, tmp_path):
        config = live_config(tmp_path, bot_token="")
        with pytest.raises(ConfigError) as caught:
            compose_live(config, clock=lambda: NOW,
                         bot=make_bot(LiveApiSession()), now_epoch=NOW)
        assert any(ENV_TOKEN in p for p in caught.value.problems)
        assert "placeholder" in str(caught.value) or "missing" in str(caught.value)


class TestCompositionWiring:
    def test_cycle_polls_durable_offset_and_records_health(self, tmp_path):
        config = live_config(tmp_path)
        session = LiveApiSession()
        live = compose_live(config, clock=lambda: NOW, bot=make_bot(session),
                            now_epoch=NOW)
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
        live = compose_live(config, clock=lambda: NOW, bot=make_bot(session),
                            now_epoch=NOW)
        try:
            report = asyncio.run(live.loop.run_cycle())
            assert report.ok and report.handled == 1
            assert live.inbox.is_done(501) and live.inbox.safe_offset() == 501
            # the owner got a menu reply — delivered via the composed flow
            assert OWNER in session.sent_chats()
            assert session.sent_to_channel() == []
        finally:
            asyncio.run(live.close())

    def test_nonowner_update_gets_no_reply_but_is_completed(self, tmp_path):
        config = live_config(tmp_path)
        session = LiveApiSession()
        session.update_script = [[start_update(601, chat_id=999, user_id=999)]]
        live = compose_live(config, clock=lambda: NOW, bot=make_bot(session),
                            now_epoch=NOW)
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
        live = compose_live(config, clock=lambda: NOW, bot=make_bot(session),
                            now_epoch=NOW)
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
        live = compose_live(config, clock=lambda: NOW, bot=make_bot(session),
                            now_epoch=NOW)
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
        live = compose_live(config, clock=lambda: NOW, bot=make_bot(session),
                            now_epoch=NOW)
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
                compose_live(config, clock=lambda: NOW,
                             bot=make_bot(LiveApiSession()), now_epoch=NOW)
        assert caught.value.__cause__ is None  # aiogram error fully suppressed
        assert BAD_TOKEN not in str(caught.value)
        assert BAD_TOKEN not in caplog.text