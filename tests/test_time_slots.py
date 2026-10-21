"""Slots, wall-clock safety and suggestion sources.

Europe/Berlin 2026 DST edges (verified against zoneinfo in tests):
gap  — 2026-03-29 02:00 -> 03:00, local 02:xx does not exist;
fold — 2026-10-25 03:00 -> 02:00, local 02:xx occurs twice.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from hypothesis import given, settings as hyp_settings, strategies as st
from zoneinfo import ZoneInfo

from poster_bot.domain.errors import (
    AmbiguousLocalTimeError,
    NonLocalTimeError,
)
from poster_bot.domain.time import (
    Instant,
    day_slots,
    local_to_instant,
    next_free_slot,
    recent_manual_suggestions,
)

MOSCOW = "Europe/Moscow"
BERLIN = "Europe/Berlin"
GAP_DAY = date(2026, 3, 29)
FOLD_DAY = date(2026, 10, 25)
NORMAL_DAY = date(2026, 10, 21)


def _epoch_local(dt: datetime) -> int:
    return int(dt.timestamp())


class TestWallClockConversion:
    def test_round_trip_normal_time(self):
        instant = local_to_instant(NORMAL_DAY, 14, 37, MOSCOW)
        local = instant.local(MOSCOW)
        assert (local.hour, local.minute, local.date()) == (14, 37, NORMAL_DAY)

    def test_gap_rejected_explicitly(self):
        with pytest.raises(NonLocalTimeError) as exc_info:
            local_to_instant(GAP_DAY, 2, 30, BERLIN)
        assert "does not exist" in str(exc_info.value)

    def test_fold_rejected_explicitly(self):
        with pytest.raises(AmbiguousLocalTimeError) as exc_info:
            local_to_instant(FOLD_DAY, 2, 30, BERLIN)
        assert "occurs twice" in str(exc_info.value)

    def test_gap_and_fold_absent_in_moscow(self):
        # Europe/Moscow has no DST in the modern era: both map normally.
        local_to_instant(GAP_DAY, 2, 30, MOSCOW)
        local_to_instant(FOLD_DAY, 2, 30, MOSCOW)


class TestDaySlots:
    def test_hours_inclusive(self):
        slots = day_slots(NORMAL_DAY, MOSCOW, 9, 22)
        locals = [s.local(MOSCOW) for s in slots]
        assert [t.hour for t in locals] == list(range(9, 23))
        assert all(t.minute == 0 for t in locals)

    def test_gap_hour_skipped(self):
        slots = day_slots(GAP_DAY, BERLIN, 0, 5)
        hours = [s.local(BERLIN).hour for s in slots]
        assert 2 not in hours
        assert hours == [0, 1, 3, 4, 5]

    def test_fold_hour_resolves_to_first_occurrence(self):
        slots = day_slots(FOLD_DAY, BERLIN, 1, 3)
        two = next(s for s in slots if s.local(BERLIN).hour == 2)
        # fold=0 first occurrence: still on summer offset +02:00
        assert two.local(BERLIN).utcoffset() == timedelta(hours=2)
        assert two == local_to_instant_safe(FOLD_DAY, 2, 0)

    def test_strictly_increasing_with_fold(self):
        slots = day_slots(FOLD_DAY, BERLIN, 0, 23)
        epochs = [s.epoch for s in slots]
        assert epochs == sorted(set(epochs))

    @given(
        start=st.integers(0, 12),
        span=st.integers(0, 23),
    )
    @hyp_settings(max_examples=40)
    def test_slots_monotonic_within_hours(self, start, span):
        end = min(start + span, 23)
        slots = day_slots(NORMAL_DAY, MOSCOW, start, end)
        assert len(slots) == end - start + 1
        epochs = [s.epoch for s in slots]
        assert epochs == sorted(epochs) and len(set(epochs)) == len(epochs)
        for slot in slots:
            assert start <= slot.local(MOSCOW).hour <= end
            assert slot.local(MOSCOW).minute == 0


def local_to_instant_safe(day, hh, mm):
    """Slot-style resolution (machine choice): folds take fold=0."""
    tz = ZoneInfo(BERLIN)
    naive = datetime(day.year, day.month, day.day, hh, mm)
    return Instant(int(naive.replace(tzinfo=tz).timestamp()))


class TestNextFreeSlot:
    def test_strictly_future_boundary(self):
        slots = day_slots(NORMAL_DAY, MOSCOW, 9, 22)
        at_slot = slots[3].epoch
        chosen = next_free_slot(at_slot, MOSCOW, 9, 22)
        assert chosen.epoch > at_slot  # == now is NOT acceptable
        assert chosen.epoch == slots[4].epoch

    def test_reserved_minutes_skipped(self):
        slots = day_slots(NORMAL_DAY, MOSCOW, 9, 22)
        at_slot = slots[3].epoch
        reserved = {slots[4].epoch}
        chosen = next_free_slot(at_slot, MOSCOW, 9, 22, reserved)
        assert chosen.epoch == slots[5].epoch

    def test_wraps_to_next_day_after_last_slot(self):
        last = day_slots(NORMAL_DAY, MOSCOW, 9, 22)[-1]
        chosen = next_free_slot(last.epoch, MOSCOW, 9, 22)
        assert chosen.local(MOSCOW).date() == NORMAL_DAY + timedelta(days=1)
        assert chosen.local(MOSCOW).hour == 9

    def test_narrow_hours_respected(self):
        now = _epoch_local(datetime(2026, 10, 21, 23, 30, tzinfo=ZoneInfo(MOSCOW)))
        chosen = next_free_slot(now, MOSCOW, 9, 10)
        assert chosen.local(MOSCOW).hour == 9


class TestManualSuggestions:
    def test_separate_source_and_future_mapping(self):
        now = _epoch_local(datetime(2026, 10, 21, 9, 0, tzinfo=ZoneInfo(MOSCOW)))
        suggestions = recent_manual_suggestions([(14, 37), (8, 5)], now, MOSCOW)
        mapping = {(s.hh, s.mm): s for s in suggestions}
        assert set(mapping) == {(14, 37), (8, 5)}
        assert all(s.source == "manual" for s in suggestions)
        # 14:37 still today; 08:05 already passed -> tomorrow
        assert mapping[(14, 37)].instant.local(MOSCOW).date() == NORMAL_DAY
        assert mapping[(8, 5)].instant.local(MOSCOW).date() == NORMAL_DAY + timedelta(days=1)
        assert all(s.instant.epoch > now for s in suggestions)

    def test_ambiguous_occurrences_skipped(self):
        # Berlin 2026-10-24 23:00 local; 02:30 on the fold day is ambiguous,
        # so the suggestion must skip to 2026-10-26.
        now = _epoch_local(datetime(2026, 10, 24, 23, 0, tzinfo=ZoneInfo(BERLIN)))
        (suggestion,) = recent_manual_suggestions([(2, 30)], now, BERLIN)
        assert suggestion.instant.local(BERLIN).date() == date(2026, 10, 26)

    def test_limit_respected(self):
        now = _epoch_local(datetime(2026, 10, 21, 9, 0, tzinfo=ZoneInfo(MOSCOW)))
        entries = [(h, m) for h in range(24) for m in (0, 30)]
        assert len(recent_manual_suggestions(entries, now, MOSCOW, limit=4)) == 4