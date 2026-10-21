"""Time domain: UTC-stable instants, hourly slots, DST-safe wall-clock mapping.

Storage invariant: every scheduled instant lives as a UTC epoch. Changing the
timezone setting or slot hours never moves existing publications — wall-clock
meaning is re-derived at presentation time only.

DST policy:
- owner-entered wall-clock time that does not exist (gap)  -> rejected;
- owner-entered wall-clock time that occurs twice (fold)   -> rejected,
  the owner must pick an unambiguous minute;
- generated hourly slots are machine-chosen: gap hours are skipped, fold hours
  resolve to the first (fold=0) occurrence, so slot lists are deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from .errors import (
    AmbiguousLocalTimeError,
    NonLocalTimeError,
    NoSlotAvailableError,
)

HORIZON_DAYS = 400


def minute_of(epoch: int) -> int:
    return epoch - (epoch % 60)


@dataclass(frozen=True, order=True)
class Instant:
    """A UTC instant; ordering is by epoch."""

    epoch: int

    @property
    def minute(self) -> int:
        return minute_of(self.epoch)

    @property
    def iso(self) -> str:
        return datetime.fromtimestamp(self.epoch, timezone.utc).isoformat()

    def local(self, tzname: str) -> datetime:
        return datetime.fromtimestamp(self.epoch, timezone.utc).astimezone(
            ZoneInfo(tzname)
        )


def _resolve(day: date, hh: int, mm: int, tz: ZoneInfo) -> tuple[Instant, str]:
    """Return (instant, status) with status 'ok' | 'gap' | 'fold' (fold=0)."""
    naive = datetime(day.year, day.month, day.day, hh, mm)
    first = naive.replace(tzinfo=tz)  # fold=0
    second = naive.replace(tzinfo=tz, fold=1)
    utc0 = first.astimezone(timezone.utc)
    utc1 = second.astimezone(timezone.utc)
    back = utc0.astimezone(tz)
    if (back.hour, back.minute, back.date()) != (hh, mm, day):
        return Instant(int(utc0.timestamp())), "gap"
    if utc1 != utc0:
        return Instant(int(utc0.timestamp())), "fold"
    return Instant(int(utc0.timestamp())), "ok"


def local_to_instant(day: date, hh: int, mm: int, tzname: str) -> Instant:
    """Owner-entered wall-clock time -> UTC instant, unambiguous only."""
    instant, status = _resolve(day, hh, mm, ZoneInfo(tzname))
    if status == "gap":
        raise NonLocalTimeError(
            f"{day.isoformat()} {hh:02d}:{mm:02d} does not exist in {tzname} "
            "(clocks skip it); choose another minute"
        )
    if status == "fold":
        raise AmbiguousLocalTimeError(
            f"{day.isoformat()} {hh:02d}:{mm:02d} occurs twice in {tzname}; "
            "choose a minute that is not ambiguous"
        )
    return instant


def day_slots(day: date, tzname: str, start_hour: int, end_hour: int) -> tuple[Instant, ...]:
    """Hourly slots (minute 00) for hours start..end inclusive, DST-safe."""
    tz = ZoneInfo(tzname)
    out: list[Instant] = []
    for hour in range(start_hour, end_hour + 1):
        instant, status = _resolve(day, hour, 0, tz)
        if status == "gap":
            continue
        if not out or instant.epoch > out[-1].epoch:
            out.append(instant)
    return tuple(out)


def next_free_slot(
    now_epoch: int,
    tzname: str,
    start_hour: int,
    end_hour: int,
    reserved: frozenset[int] | set[int] = frozenset(),
    *,
    horizon_days: int = HORIZON_DAYS,
) -> Instant:
    """Smallest strictly-future unreserved slot instant (boundary: == now fails)."""
    day = datetime.fromtimestamp(now_epoch, timezone.utc).astimezone(
        ZoneInfo(tzname)
    ).date()
    for offset in range(horizon_days):
        for slot in day_slots(day + timedelta(days=offset), tzname, start_hour, end_hour):
            if slot.epoch > now_epoch and slot.epoch not in reserved:
                return slot
    raise NoSlotAvailableError(
        f"no free hourly slot within {horizon_days} days in {tzname}"
    )


@dataclass(frozen=True)
class ManualSuggestion:
    instant: Instant
    hh: int
    mm: int
    source: str = "manual"  # separate source from hourly slots by contract


def recent_manual_suggestions(
    entries: Sequence[tuple[int, int]],
    now_epoch: int,
    tzname: str,
    *,
    limit: int = 5,
    horizon_days: int = HORIZON_DAYS,
) -> list[ManualSuggestion]:
    """Next strictly-future unambiguous occurrence per recent owner-entered
    hh:mm value (ordered as given). Ambiguous/nonexistent day occurrences are
    skipped until an unambiguous one. Entries are a separate suggestion source
    from hourly slots."""
    tz = ZoneInfo(tzname)
    today = datetime.fromtimestamp(now_epoch, timezone.utc).astimezone(tz).date()
    out: list[ManualSuggestion] = []
    for hh, mm in entries[: limit * 3]:
        if len(out) >= limit:
            break
        for offset in range(horizon_days):
            instant, status = _resolve(today + timedelta(days=offset), hh, mm, tz)
            if status != "ok":
                continue
            if instant.epoch > now_epoch:
                out.append(ManualSuggestion(instant=instant, hh=hh, mm=mm))
                break
            # unambiguous but not future today: look at the next day
            continue
    return out


def upcoming_instants_from_entries(
    entries: Iterable[tuple[int, int]], now_epoch: int, tzname: str
) -> list[Instant]:
    return [s.instant for s in recent_manual_suggestions(entries, now_epoch, tzname)]