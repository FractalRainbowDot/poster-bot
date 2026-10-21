"""Settings changes never move existing publications; invalid changes rejected."""
from __future__ import annotations

import pytest

from conftest import NOW, future, text_content
from poster_bot.config import load_config
from poster_bot.repo.settings import SettingsError


class TestSeedAndChange:
    def test_seed_from_config(self, services):
        services.settings.seed_defaults(load_config({}))
        got = services.settings.get()
        assert (got.timezone, got.start_hour, got.end_hour) == (
            "Europe/Moscow",
            0,
            23,
        )

    def test_seed_does_not_overwrite_owner_changes(self, services):
        services.settings.seed_defaults(load_config({}))
        services.settings.change(timezone="Europe/Berlin")
        services.settings.seed_defaults(load_config({"POSTER_BOT_TIMEZONE": "Asia/Seoul"}))
        assert services.settings.get().timezone == "Europe/Berlin"

    def test_publications_stable_across_changes(self, services, scheduled):
        services.settings.seed_defaults(load_config({}))
        pub, _ = scheduled
        before = services.publications.get(pub.id)
        services.settings.change(timezone="Asia/Almaty", slot_start_hour=6, slot_end_hour=8)
        services.settings.change(timezone="America/New_York")
        after = services.publications.get(pub.id)
        assert after.scheduled_epoch == before.scheduled_epoch
        assert after.minute_epoch == before.minute_epoch
        assert after.status is before.status

    def test_invalid_values_rejected(self, services):
        services.settings.seed_defaults(load_config({}))
        for kwargs in (
            {"slot_start_hour": 24},
            {"timezone": "Mars/Olympus"},
            {"slot_start_hour": 20, "slot_end_hour": 10},
            {"bot_token": "sneaky"},
        ):
            with pytest.raises(SettingsError):
                services.settings.change(**kwargs)
        # nothing half-applied
        got = services.settings.get()
        assert (got.timezone, got.start_hour, got.end_hour) == (
            "Europe/Moscow",
            0,
            23,
        )


class TestSlotFunctionsUseSettings:
    def test_new_suggestions_follow_new_hours(self, services, scheduled):
        from poster_bot.domain.time import day_slots
        from datetime import date

        services.settings.seed_defaults(load_config({}))
        services.settings.change(slot_start_hour=11, slot_end_hour=12)
        day = date(2026, 10, 21)
        got = services.settings.get()
        hours = [
            s.local(got.timezone).hour
            for s in day_slots(day, got.timezone, got.start_hour, got.end_hour)
        ]
        assert hours == [11, 12]