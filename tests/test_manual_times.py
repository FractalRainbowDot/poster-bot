"""Recent manual hh:mm store: distinct values, newest first, valid range."""
from __future__ import annotations

import pytest

from conftest import NOW
from poster_bot.domain.errors import DomainError


class TestManualTimes:
    def test_record_and_recent_order(self, services):
        store = services.manual_times
        store.record(14, 37, now_epoch=NOW)
        store.record(9, 5, now_epoch=NOW + 60)
        store.record(14, 37, now_epoch=NOW + 120)  # upsert bumps recency
        assert store.recent(limit=5) == [(14, 37), (9, 5)]

    def test_range_validation(self, services):
        for hh, mm in ((24, 0), (0, 60), (-1, 0)):
            with pytest.raises(DomainError):
                services.manual_times.record(hh, mm, now_epoch=NOW)

    def test_suggestions_are_separate_manual_source(self, services):
        from poster_bot.domain.time import recent_manual_suggestions

        services.manual_times.record(23, 45, now_epoch=NOW)
        (suggestion,) = recent_manual_suggestions(
            services.manual_times.recent(), NOW, "Europe/Moscow"
        )
        assert suggestion.source == "manual"
        assert (suggestion.hh, suggestion.mm) == (23, 45)