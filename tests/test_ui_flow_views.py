"""Views: day-plan navigation/pagination, journal pagination, settings editor."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from conftest import NOW, text_content
from ui_flow_helpers import build_flow, labels, press_label, run

from poster_bot.domain.states import Status
from poster_bot.domain.time import Instant
import poster_bot.ui.texts as texts

MOSCOW = ZoneInfo("Europe/Moscow")


class TestDayPlan:
    def test_navigation_between_days_shows_rows_and_statuses(self, services):
        flow, _ = build_flow(services)
        post_id = services.drafts.create(text_content(), now_epoch=NOW)
        tomorrow_noon = int(datetime(2026, 10, 22, 12, 0, tzinfo=MOSCOW).timestamp())
        services.publications.schedule(post_id, Instant(tomorrow_noon),
                                       now_epoch=NOW)

        today = flow.plan_today(now=NOW)
        assert texts.NO_PLANS_DAY in today.text
        nav = labels(today)
        assert "← Пред." in nav and "Сегодня" in nav and "След. →" in nav

        tomorrow = press_label(flow, today, "След. →", now=NOW)
        assert texts.PLAN_DAY_PREFIX in tomorrow.text
        scheduled_row = next(l for l in labels(tomorrow)
                             if texts.STATUS_RU[Status.SCHEDULED] in l)
        assert "№" in scheduled_row

        back_today = press_label(flow, tomorrow, "Сегодня", now=NOW)
        assert texts.NO_PLANS_DAY in back_today.text

    def test_day_pagination_within_4096_budget(self, services):
        flow, _ = build_flow(services)
        # 7 scheduled pubs on 21.10 exceed PAGE_SIZE=6 and must split
        for i in range(7):
            pid = services.drafts.create(text_content(), now_epoch=NOW)
            services.publications.schedule(
                pid, Instant(NOW + 3600 + i * 600), now_epoch=NOW)
        page1 = flow.plan_today(now=NOW)
        assert "1–6 из 7" in page1.text
        assert len(page1.text) <= 4096
        assert any(l.startswith("Ещё ↓") for l in labels(page1))
        page2 = press_label(flow, page1, "Ещё ↓ 2/2", now=NOW)
        assert "7–7 из 7" in page2.text
        assert any(l == "↑ Сверху" for l in labels(page2))
        # "След. →" теперь всегда следующий день, а не страница
        nxt = press_label(flow, page2, "След. →", now=NOW)
        assert "22.10.2026" in nxt.text and texts.NO_PLANS_DAY in nxt.text


class TestJournal:
    def test_pagination_and_no_post_contents(self, services):
        flow, _ = build_flow(services)
        for i in range(9):
            pid = services.drafts.create(text_content(), now_epoch=NOW)
            services.journal.record("draft_created", post_id=pid, now_epoch=NOW)
        page1 = flow.journal_view(now=NOW, page=0)
        assert f"{texts.TITLE_JOURNAL} (1–6 из 9)".rstrip(":") in page1.text
        page2 = press_label(flow, page1, "След. →", now=NOW)
        assert "7–9 из 9" in page2.text
        # journal lines carry no post contents
        assert "hello owner" not in page1.text + page2.text


class TestSettings:
    def _tz_editor(self, flow):
        view = flow.settings_view(now=NOW)
        assert view.text.startswith(texts.TITLE_SETTINGS)
        assert "Europe/Moscow" in view.text
        ask = press_label(flow, view, texts.SET_TZ_LABEL, now=NOW)
        assert texts.SETTINGS_VALUE_ASK in ask.text
        return ask

    def test_timezone_round_trip_and_journal_record(self, services):
        flow, _ = build_flow(services)
        self._tz_editor(flow)
        done = run(flow.text(now=NOW, text="Europe/Berlin"))
        assert "Europe/Berlin" in done.text
        assert services.settings.get().timezone == "Europe/Berlin"
        view = flow.settings_view(now=NOW)
        assert "Europe/Berlin" in view.text
        entries = services.journal.entries_page(0, 20)
        kinds = [row["kind"] for row in entries]
        assert "settings_changed" in kinds

    def test_bad_timezone_rejected_without_change(self, services):
        flow, _ = build_flow(services)
        self._tz_editor(flow)
        bad = run(flow.text(now=NOW, text="Moscow/Time"))
        assert bad.text.startswith(texts.SETTINGS_BAD_TZ)
        assert services.settings.get().timezone == "Europe/Moscow"

    def test_hour_bounds_validated_across_keys(self, services):
        flow, _ = build_flow(services)
        view = flow.settings_view(now=NOW)
        press_label(flow, view, texts.SET_END_LABEL, now=NOW)
        done = run(flow.text(now=NOW, text="10"))
        assert ": 10" in done.text
        # start 22 would exceed the new end 10
        view = flow.settings_view(now=NOW)
        press_label(flow, view, texts.SET_START_LABEL, now=NOW)
        bad = run(flow.text(now=NOW, text="22"))
        assert bad.text.startswith(texts.SETTINGS_BAD_HOUR)
        current = services.settings.get()
        assert (current.start_hour, current.end_hour) == (0, 10)

    def test_settings_change_does_not_move_publications(self, services):
        from conftest import future

        flow, _ = build_flow(services)
        pid = services.drafts.create(text_content(), now_epoch=NOW)
        pub = services.publications.schedule(pid, future(120), now_epoch=NOW)
        self._tz_editor(flow)
        run(flow.text(now=NOW, text="Asia/Almaty"))
        assert services.publications.get(pub.id).minute_epoch == pub.minute_epoch