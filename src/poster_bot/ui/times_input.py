"""Owner-entered wall-clock time parsing (ЧЧ:ММ and ДД.ММ.ГГГГ ЧЧ:ММ).

Both forms resolve to a strictly-future unambiguous instant in the configured
timezone, reusing the domain rules (DST gap/fold rejections). ЧЧ:ММ means the
next unambiguous future occurrence of that wall-clock minute.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..domain.errors import DomainError, PastTimeError
from ..domain.time import (
    Instant,
    local_to_instant,
    recent_manual_suggestions,
)

_SHORT = re.compile(r"^(\d{1,2}):(\d{2})$")
_LONG = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4}) (\d{1,2}):(\d{2})$")


def parse_clock(raw: str) -> tuple[int, int] | None:
    """'hh:mm' text -> (hh, mm) shape check only (calendar-free)."""
    match = _SHORT.match(raw.strip())
    if not match:
        return None
    hh, mm = int(match.group(1)), int(match.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def resolve_manual_time(
    raw: str, now_epoch: int, tzname: str
) -> tuple[Instant, int, int]:
    """Owner time text -> (instant, hh, mm). Raises DomainError subclasses
    for bad shape / non-local / ambiguous / past input."""
    text = raw.strip()
    long_match = _LONG.match(text)
    if long_match:
        dd, mo, yyyy, hh, mm = (
            int(long_match.group(i)) for i in range(1, 6)
        )
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise DomainError("часы должны быть 0–23, минуты 0–59")
        try:
            day = date(yyyy, mo, dd)
        except ValueError:
            raise DomainError("такой даты нет") from None
        instant = local_to_instant(day, hh, mm, tzname)
        if instant.epoch <= now_epoch:
            raise PastTimeError("время публикации должно быть строго в будущем")
        return instant, hh, mm
    clock = parse_clock(text)
    if clock is None:
        raise DomainError(
            "нужно время в формате ЧЧ:ММ или ДД.ММ.ГГГГ ЧЧ:ММ"
        )
    hh, mm = clock
    suggestions = recent_manual_suggestions(
        [(hh, mm)], now_epoch, tzname, limit=1
    )
    if not suggestions:
        raise PastTimeError(
            "для такого времени нет будущего момента в горизонте поиска"
        )
    return suggestions[0].instant, hh, mm


def format_local(instant: Instant, tzname: str) -> str:
    """Display form for the owner: DD.MM.YYYY HH:MM + zone."""
    moment = datetime.fromtimestamp(instant.epoch, timezone.utc).astimezone(
        ZoneInfo(tzname)
    )
    return moment.strftime("%d.%m.%Y %H:%M")


def day_bounds(day: date, tzname: str) -> tuple[int, int]:
    """[start, end) UTC epochs of a local calendar day (fold=0 boundaries)."""
    tz = ZoneInfo(tzname)
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def local_day(now_epoch: int, tzname: str) -> date:
    return datetime.fromtimestamp(now_epoch, timezone.utc).astimezone(
        ZoneInfo(tzname)
    ).date()