"""Views: day-plan navigation/pagination, journal pagination, settings editor."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest

from conftest import NOW, text_content
from ui_flow_helpers import build_flow, labels, press_label, run

from poster_bot.domain.states import Status
from poster_bot.domain.content import Content, MediaRef, ForwardSource
from poster_bot.domain.time import Instant
import poster_bot.ui.texts as texts

MOSCOW = ZoneInfo("Europe/Moscow")


class TestDayPlan:
    def test_one_hundred_mixed_rows_reachable_across_ten_pages(self, services):
        flow, _ = build_flow(services)
        expected = []
        for i in range(100):
            pid = services.drafts.create(text_content(f"post-{i:03d}"), now_epoch=NOW)
            pub = services.publications.schedule(pid, Instant(NOW + 60 * (i + 1)), now_epoch=NOW)
            if i % 3 == 0:
                attempt = services.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch)
                services.dispatch.complete(pub.id, attempt, (i + 1000,),
                    f"https://t.me/c/555/{i + 1000}", now_epoch=pub.scheduled_epoch)
            elif i % 3 == 1:
                services.publications.pause(pub.id, pub.version, now_epoch=NOW)
            expected.append(f"post-{i:03d}")
        assert len(run(flow.command_start(now=NOW)).rows) == 13
        view = flow.plan_today(now=NOW)
        seen = []
        for page in range(10):
            rows = [row[0].label for row in view.rows if " | " in row[0].label]
            assert len(rows) == 10
            assert len(view.rows) == 12  # entries and both navigation rows survive
            assert f"{page * 10 + 1}–{(page + 1) * 10} из 100" in view.text
            seen.extend(label.rsplit(" | ", 1)[1] for label in rows)
            if page < 9:
                view = press_label(flow, view, f"Ещё ↓ {page + 2}/10", now=NOW)
        assert seen == expected and len(set(seen)) == 100
        assert not any(label.startswith("Ещё ↓") for label in labels(view))
        first = press_label(flow, view, "↑ Сверху", now=NOW)
        assert "1–10 из 100" in first.text
        tomorrow = press_label(flow, first, "След. →", now=NOW)
        assert "22.10.2026" in tomorrow.text and texts.NO_PLANS_DAY in tomorrow.text

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
        assert "12:00 | 🕒 | hello owner" in labels(tomorrow)

        back_today = press_label(flow, tomorrow, "Сегодня", now=NOW)
        assert texts.NO_PLANS_DAY in back_today.text

    def test_day_pagination_within_4096_budget(self, services):
        flow, _ = build_flow(services)
        # Only the content plan uses ten entries per page.
        for i in range(11):
            pid = services.drafts.create(text_content(), now_epoch=NOW)
            services.publications.schedule(
                pid, Instant(NOW + 3600 + i * 600), now_epoch=NOW)
        page1 = flow.plan_today(now=NOW)
        assert "1–10 из 11" in page1.text
        assert len(page1.text) <= 4096
        assert any(l.startswith("Ещё ↓") for l in labels(page1))
        page2 = press_label(flow, page1, "Ещё ↓ 2/2", now=NOW)
        assert "11–11 из 11" in page2.text
        assert page2.rows[0][0].label.startswith("13:40 | 🕒 |")
        assert not any(l.startswith("Ещё ↓") for l in labels(page2))
        assert any(l == "↑ Сверху" for l in labels(page2))
        # "След. →" теперь всегда следующий день, а не страница
        nxt = press_label(flow, page2, "След. →", now=NOW)
        assert "22.10.2026" in nxt.text and texts.NO_PLANS_DAY in nxt.text

    @pytest.mark.parametrize("status", list(Status))
    def test_row_local_time_status_and_stored_text(self, services, status):
        flow, _ = build_flow(services)
        services.settings.change(timezone="Pacific/Honolulu")
        when = int(datetime(2026, 10, 22, 8, 10,
                            tzinfo=ZoneInfo("Pacific/Honolulu")).timestamp())
        pid = services.drafts.create(text_content("  actual\n publication\ttext  "),
                                     now_epoch=NOW)
        pub = services.publications.schedule(pid, Instant(when), now_epoch=NOW)
        if status is Status.SUSPENDED:
            services.publications.pause(pub.id, pub.version, now_epoch=NOW)
        elif status not in (Status.SCHEDULED, Status.SUSPENDED):
            attempt = services.dispatch.claim(pub.id, pub.version, now_epoch=when)
            services.dispatch.mark_in_progress(attempt, now_epoch=when)
            if status is Status.PUBLISHED:
                services.dispatch.complete(pub.id, attempt, (999,),
                    "https://t.me/c/555/999", now_epoch=when)
            elif status is Status.UNCERTAIN:
                services.dispatch.mark_uncertain(pub.id, attempt, "timeout", now_epoch=when)
            elif status is Status.MISSED:
                services.dispatch.fail_definitive(pub.id, attempt, "api_error", now_epoch=when)
        view = flow.plan_day(now=NOW, day_iso="2026-10-22", page=0)
        mark = {Status.SCHEDULED: "🕒", Status.SUSPENDED: "⏸",
                Status.SENDING: "⏳", Status.PUBLISHED: "✅",
                Status.UNCERTAIN: "❓", Status.MISSED: "❌"}[status]
        assert view.rows[0][0].label == f"08:10 | {mark} | actual publication text"
        assert len(view.rows[0]) == 1  # including published history with a permalink
        assert view.rows[0][0].token and not view.rows[0][0].url
        assert services.drafts.get_content(pid).text == "  actual\n publication\ttext  "

    @pytest.mark.parametrize("content,excerpt", [
        (Content(kind="photo", media=MediaRef("photo", "fake", "photo\ncaption")), "photo caption"),
        (Content(kind="video", media=MediaRef("video", "fake")), "Видео"),
        (Content(kind="audio", media=MediaRef("audio", "fake", "song title")), "song title"),
        (Content(kind="album", items=(MediaRef("photo", "one"), MediaRef("video", "two")),
                 caption="album caption"), "album caption"),
        (Content(kind="album", items=(MediaRef("photo", "one"),
                 MediaRef("video", "two", "item caption"))), "item caption"),
        (Content(kind="album", items=(MediaRef("photo", "one"), MediaRef("photo", "two"))), "Альбом"),
        (Content(kind="forward", mode="forward", forward=ForwardSource(-100555, (42,))),
         "Пересылка источника"),
    ])
    def test_caption_and_readable_fallback_without_source_fetch(self, services, content, excerpt):
        flow, preview = build_flow(services)
        pid = services.drafts.create(content, now_epoch=NOW)
        services.publications.schedule(pid, Instant(NOW + 60), now_epoch=NOW)
        view = flow.plan_today(now=NOW)
        assert view.rows[0][0].label == f"11:01 | 🕒 | {excerpt}"
        assert not preview.calls
        assert services.drafts.get_content(pid) == content

    def test_all_thirteen_rows_reachable_in_time_order_and_date_navigation(self, services):
        flow, _ = build_flow(services)
        for i in reversed(range(13)):
            pid = services.drafts.create(text_content(f"post-{i:02d}"), now_epoch=NOW)
            services.publications.schedule(pid, Instant(NOW + 60 * (i + 1)), now_epoch=NOW)
        view = flow.plan_today(now=NOW)
        seen = []
        for page in range(2):
            post_rows = [row for row in view.rows if " | 🕒 | " in row[0].label]
            assert all(len(row) == 1 for row in post_rows)
            assert len(view.rows) == (12 if page == 0 else 5)
            seen.extend(row[0].label for row in post_rows)
            if page < 1:
                view = press_label(flow, view, "Ещё ↓", now=NOW)
        assert seen == [f"11:{i + 1:02d} | 🕒 | post-{i:02d}" for i in range(13)]
        next_day = press_label(flow, view, "След. →", now=NOW)
        assert "22.10.2026" in next_day.text and texts.NO_PLANS_DAY in next_day.text
        previous = press_label(flow, next_day, "← Пред.", now=NOW)
        assert previous.rows[0][0].label == seen[0]

    @pytest.mark.parametrize("count", [0, 1, 10])
    def test_single_page_boundaries_in_utc(self, services, count):
        flow, _ = build_flow(services)
        services.settings.change(timezone="UTC")
        for i in range(count):
            pid = services.drafts.create(text_content(f"post-{i:02d}"), now_epoch=NOW)
            services.publications.schedule(pid, Instant(NOW + 60 * (i + 1)), now_epoch=NOW)
        view = flow.plan_today(now=NOW)
        assert "(UTC)" in view.text
        assert len(view.rows) == count + 2
        assert not any(label.startswith(("Ещё ↓", "↑ Сверху")) for label in labels(view))
        if count:
            assert f"1–{count} из {count}" in view.text
            assert view.rows[0][0].label == "08:01 | 🕒 | post-00"
        else:
            assert texts.NO_PLANS_DAY in view.text

    def test_excerpt_bounded_and_payload_untouched(self, services):
        flow, _ = build_flow(services)
        original = "long\n\u202e" + "text" * 900
        pid = services.drafts.create(text_content(original), now_epoch=NOW)
        services.publications.schedule(pid, Instant(NOW + 60), now_epoch=NOW)
        label = flow.plan_today(now=NOW).rows[0][0].label
        assert label.startswith("11:01 | 🕒 | long text")
        assert label.endswith("…") and len(label) <= 92
        assert "\n" not in label and "\u202e" not in label
        assert services.drafts.get_content(pid).text == original


class TestJournal:
    def test_draft_list_keeps_six_entries_per_page(self, services):
        flow, _ = build_flow(services)
        for i in range(11):
            services.drafts.create(text_content(f"draft-{i}"), now_epoch=NOW)
        page1 = flow.drafts_view(now=NOW, page=0)
        assert "1–6 из 11" in page1.text
        assert len(page1.rows) == 7
        page2 = press_label(flow, page1, "След. →", now=NOW)
        assert "7–11 из 11" in page2.text
        assert len(page2.rows) == 6

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
        ask = press_label(flow, ask, "Ввести IANA вручную", now=NOW)
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
