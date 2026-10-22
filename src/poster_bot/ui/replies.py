"""Flow output: owner replies with server-token buttons, render-agnostic.

The flow engine produces these plain data objects; the aiogram adapter turns
them into messages/keyboards. Keeping them data-only means every owner-visible
decision is testable offline without aiogram types.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from .dashboard import Dashboard

#: Telegram hard limit for message texts
TEXT_MAX = 4096
#: Application guard, not a Telegram hard limit: twelve single-column planned
#: entries plus Journal. Paginated draft/day lists keep their smaller page size.
MAX_ROWS = 13
#: draft/journal list entries per page
PAGE_SIZE = 6
#: content-plan publications per day page (independent of the main dashboard).
DAY_PAGE_SIZE = 10


@dataclass(frozen=True)
class Button:
    label: str
    token: str = ""  # action token issued by ActionStore; callback data stays short
    url: str | None = None  # history/permalink links carry a URL, never a token

    def __post_init__(self) -> None:
        if bool(self.token) == (self.url is not None):
            raise ValueError("a button needs exactly one of token or url")


@dataclass(frozen=True)
class Reply:
    """One owner-facing message plus its inline keyboard."""

    text: str
    rows: tuple[tuple[Button, ...], ...] = field(default_factory=tuple)
    # A rejected callback is a toast, never a replacement for the active card.
    stale_callback: bool = False
    # Successful non-content input; flow records a durable per-message receipt
    # before the adapter may remove the owner's incoming message.
    input_accepted: bool = False
    # Server-owned received sources released by an explicit persisted copy.
    # Adapter additionally checks owner chat and durable intake receipt.
    released_inputs: tuple[tuple[int, int], ...] = ()
    # Main-card identity metadata; never used to change business state.
    dashboard: Dashboard | None = None
    dashboard_version: int | None = None

    def __post_init__(self) -> None:
        if len(self.text) > TEXT_MAX:
            raise ValueError(
                f"owner reply is {len(self.text)} chars, Telegram limit is "
                f"{TEXT_MAX}; lists must paginate instead of overflowing"
            )
        if len(self.rows) > MAX_ROWS:
            raise ValueError("keyboard exceeds the row budget")


def page_window(total: int, page: int, size: int = PAGE_SIZE) -> tuple[int, int, int]:
    """(start, end, page_count) for a paginated list; clamps the page."""
    pages = max(1, (total + size - 1) // size)
    page = max(0, min(page, pages - 1))
    return page * size, min(total, (page + 1) * size), pages


def nav_rows(
    *, today_token: str, prev_token: str, next_token: str, back_token: str,
    today_label: str = "Сегодня", has_prev: bool = True, has_next: bool = True,
) -> tuple[tuple[Button, ...], ...]:
    nav = [Button("← Пред.", prev_token)] if has_prev else []
    nav.append(Button(today_label, today_token))
    if has_next:
        nav.append(Button("След. →", next_token))
    return (tuple(nav), (Button("Назад", back_token),))
