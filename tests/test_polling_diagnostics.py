"""Offline error causes, request timing and read-only polling retry policy."""
from __future__ import annotations

import logging
import socket

import pytest
from aiogram.exceptions import (
    TelegramAPIError, TelegramNetworkError, TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import GetUpdates
from conftest import NOW
from polling_helpers import build_harness, run

from poster_bot.live import AiogramUpdateSource
from poster_bot.polling import PollFailure

SECRET = "TOKEN/PRIVATE_PAYLOAD_DO_NOT_LOG"


class FailingBot:
    def __init__(self, exc):
        self.exc = exc

    async def get_updates(self, **kwargs):
        raise self.exc


@pytest.mark.parametrize("cause,category", [
    (TimeoutError(SECRET), "timeout"),
    (socket.gaierror(SECRET), "dns"),
    (ConnectionRefusedError(SECRET), "connect"),
    (None, "network"),
])
def test_network_diagnostics_safe_timed_and_cause_based(
    tmp_path, caplog, cause, category
):
    error = TelegramNetworkError(method=GetUpdates(), message=SECRET)
    error.__cause__ = cause
    times = iter([10.0, 25.0])  # simulate the 15-second session timeout
    source = AiogramUpdateSource(FailingBot(error), monotonic=lambda: next(times))
    h = build_harness(tmp_path)
    h.loop._source = source
    try:
        with caplog.at_level(logging.WARNING):
            report = run(h.loop.run_cycle())
        assert report.failure_code == "transport_error"
        assert report.failure_class == "TelegramNetworkError"
        assert report.failure_category == category
        assert report.request_duration_ms == 15000
        assert f"category={category}" in caplog.text
        assert "class=TelegramNetworkError" in caplog.text
        assert "duration_ms=15000" in caplog.text
        assert "retry_seconds=1" in caplog.text
        assert SECRET not in caplog.text + repr(report)
        assert not report.ticked and h.transport.calls == []
    finally:
        h.app.close()


@pytest.mark.parametrize("error,code,category,delay", [
    (TelegramServerError(method=GetUpdates(), message=SECRET),
     "transport_error", "server", 1),
    (TelegramAPIError(method=GetUpdates(), message=SECRET),
     "api_error", "api", 1),
    (TelegramRetryAfter(method=GetUpdates(), message=SECRET, retry_after=47),
     "rate_limit", "rate_limit", 47),
    (ValueError(SECRET), "unclassified", "unknown", 1),
    (TimeoutError(SECRET), "timeout", "timeout", 1),
])
def test_other_poll_failures_and_retry_after(tmp_path, caplog, error, code,
                                           category, delay):
    h = build_harness(tmp_path)
    times = iter([1.0, 1.125])
    h.loop._source = AiogramUpdateSource(
        FailingBot(error), monotonic=lambda: next(times)
    )
    try:
        with caplog.at_level(logging.WARNING):
            report = run(h.loop.run_cycle())
        assert report.failure_code == code
        assert report.failure_category == category
        assert report.request_duration_ms == 125
        assert report.sleep_seconds == delay
        assert SECRET not in caplog.text + repr(report)
    finally:
        h.app.close()


def test_transient_poll_wait_is_bounded_but_api_wait_is_conservative(tmp_path):
    h = build_harness(tmp_path)
    try:
        for delay in (1, 2, 4, 4, 4, 4, 4, 4):
            h.source.script = [PollFailure("transport_error")]
            assert run(h.loop.run_cycle()).sleep_seconds == delay
        assert run(h.loop.run_cycle()).ok
        for delay in (1, 2, 4, 8, 16, 30, 30):
            h.source.script = [PollFailure("api_error")]
            assert run(h.loop.run_cycle()).sleep_seconds == delay
    finally:
        h.app.close()


def test_real_aiogram_session_preserves_retry_after_without_sending(tmp_path, caplog):
    from poster_bot.live import compose_live
    from test_live_composition import LiveApiSession, live_config, make_bot

    session = LiveApiSession()
    session.update_script = [TelegramRetryAfter(
        method=GetUpdates(), message=SECRET, retry_after=47
    )]
    live = run(compose_live(
        live_config(tmp_path), clock=lambda: NOW, now_epoch=NOW,
        bot=make_bot(session),
    ))
    try:
        with caplog.at_level(logging.WARNING):
            report = run(live.loop.run_cycle())
        assert report.failure_class == "TelegramRetryAfter"
        assert report.failure_category == "rate_limit"
        assert report.sleep_seconds == 47
        assert not report.ticked and session.sent_chats() == []
        assert SECRET not in caplog.text + repr(report)
    finally:
        run(live.close())


def test_nested_aiohttp_dns_and_cyclic_causes_are_bounded(tmp_path):
    from aiohttp import ClientConnectorError

    dns = socket.gaierror(SECRET)
    connector = ClientConnectorError(None, dns)
    connector.__cause__ = dns
    error = TelegramNetworkError(method=GetUpdates(), message=SECRET)
    error.__cause__ = connector
    dns.__cause__ = error  # malformed cycle must not hang diagnostics
    times = iter([1.0, 1.0])
    source = AiogramUpdateSource(FailingBot(error), monotonic=lambda: next(times))
    with pytest.raises(PollFailure) as caught:
        run(source.fetch(offset=None, timeout=2))
    assert caught.value.category == "dns"
    assert str(caught.value) == "transport_error"
