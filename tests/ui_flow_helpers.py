"""Shared offline helpers for owner-flow tests (no network, no live bot)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from conftest import Services
from fake_transport import FakeTransport

from poster_bot.db import connect, migrate
from poster_bot.ui.flow import OwnerFlow

#: fake channel used by flow tests (private-super-group style id)
FLOW_CHANNEL = -100555


def build_flow(
    services: Services,
    *,
    channel_id: int = FLOW_CHANNEL,
    channel_username: str | None = None,
) -> tuple[OwnerFlow, FakeTransport]:
    """OwnerFlow over the test services; previews go to the same fake
    transport so tests can prove the channel publish path is never used.

    ``channel_username`` mirrors the live composition: the numeric channel id
    stays the destination, the bare/@-prefixed username only upgrades links.
    ``channel_id`` overrides the fake channel (guard tests pass invalid
    targets explicitly)."""

    services.settings.seed_defaults(SimpleNamespace(
        timezone="Europe/Moscow", slot_start_hour=0, slot_end_hour=23,
    ))
    preview = FakeTransport()
    flow = OwnerFlow(
        conn=services.conn,
        settings=services.settings,
        drafts=services.drafts,
        publications=services.publications,
        dispatch=services.dispatch,
        manual_times=services.manual_times,
        journal=services.journal,
        preview=preview,
        channel_id=channel_id,
        channel_username=channel_username,
    )
    return flow, preview


def run(coro: Any):
    return asyncio.run(coro)


def tokens(reply) -> dict[str, str]:
    """label -> token for every token button in a Reply."""

    return {
        button.label: button.token
        for row in reply.rows
        for button in row
        if button.token
    }


def labels(reply) -> list[str]:
    return [button.label for row in reply.rows for button in row]


def find_token(reply, needle: str) -> str:
    """Token of the first button whose label contains *needle*."""

    for row in reply.rows:
        for button in row:
            if needle in button.label:
                if not button.token:
                    raise AssertionError(f"button {button.label!r} has no token")
                return button.token
    raise AssertionError(f"no button containing {needle!r} in {labels(reply)}")


def press(flow: OwnerFlow, token: str, *, now: int):
    return run(flow.press(now=now, token=token))


def press_label(flow: OwnerFlow, reply, needle: str, *, now: int):
    from poster_bot.ui import texts
    if needle in (texts.MENU_NEW_POST, texts.MENU_DRAFTS, texts.MENU_PLAN, texts.MENU_SETTINGS):
        return run(flow.menu_navigation(label=needle, now=now))
    return press(flow, find_token(reply, needle), now=now)


def reopen(services: Services, tmp_path):
    """Simulate a restart: close the connection, reopen the same file.

    Returns a brand-new Services and flow over the reopened database —
    nothing may rely on in-process memory (PLAN §6 durability).
    """

    path = str(tmp_path / "bot.sqlite3")
    services.conn.close()
    conn = connect(path)
    migrate(conn)
    fresh = Services(conn)
    flow, preview = build_flow(fresh)
    return fresh, flow, preview
