"""Owner-flow scheduling: slot picker, own-time input, recent suggestions and
confirm-time revalidation (state, minute, replay, forgery). Offline only."""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import NOW, future, text_content
from ui_flow_helpers import build_flow, find_token, labels, press, press_label, run

from poster_bot.domain.states import Status

import poster_bot.ui.texts as texts


def _post_id(reply_text: str) -> int:
    return int(re.search(r"№(\d+)", reply_text).group(1))


def _draft_created(flow):
    run(flow.new_post(now=NOW))
    staged = run(flow.text(now=NOW, text="контент для плана"))
    return press_label(flow, staged, texts.BTN_CONTINUE, now=NOW)


class TestSlotScheduling:
    def test_next_slot_happy_path_reserves_free_minute(self, services):
        flow, preview = build_flow(services)
        created = _draft_created(flow)
        post_id = _post_id(created.text)
        picker = press_label(flow, created, texts.BTN_SCHEDULE, now=NOW)
        assert picker.text == texts.PICK_TIME
        confirm = press_label(flow, picker, texts.BTN_NEXT_SLOT, now=NOW)
        assert confirm.text.startswith(texts.CONFIRM_SUMMARY_PREFIX)
        scheduled = press_label(flow, confirm, texts.BTN_CONFIRM, now=NOW)
        assert scheduled.text.startswith(texts.SCHEDULED_PREFIX)
        pub = services.publications.get_by_post(post_id)
        assert pub is not None and pub.status is Status.SCHEDULED
        # Moscow 12:00 with 00–23 slot hours == NOW + 1h, minute reserved
        assert pub.minute_epoch == NOW + 3600
        assert pub.minute_epoch % 60 == 0
        assert preview.calls == []

    def test_confirmation_replay_is_refused(self, services):
        flow, _ = build_flow(services)
        created = _draft_created(flow)
        picker = press_label(flow, created, texts.BTN_SCHEDULE, now=NOW)
        confirm = press_label(flow, picker, texts.BTN_NEXT_SLOT, now=NOW)
        token = find_token(confirm, texts.BTN_CONFIRM)
        press(flow, token, now=NOW)
        replay = press(flow, token, now=NOW + 5)
        assert replay.text == texts.BUTTON_STALE

    def test_forged_token_changes_nothing(self, services):
        flow, _ = build_flow(services)
        created = _draft_created(flow)
        post_id = _post_id(created.text)
        stale = press(flow, "a0badf00dbeef", now=NOW)
        assert stale.text == texts.BUTTON_STALE
        assert services.publications.get_by_post(post_id) is None
        assert services.drafts.is_draft(post_id)

    def test_confirm_after_leaving_step_is_stale(self, services):
        flow, _ = build_flow(services)
        created = _draft_created(flow)
        picker = press_label(flow, created, texts.BTN_SCHEDULE, now=NOW)
        confirm = press_label(flow, picker, texts.BTN_NEXT_SLOT, now=NOW)
        token = find_token(confirm, texts.BTN_CONFIRM)
        press_label(flow, confirm, texts.BTN_CANCEL, now=NOW)
        stale = press(flow, token, now=NOW + 1)
        assert stale.text == texts.BUTTON_STALE
        assert services.publications.get_by_post(_post_id(created.text)) is None


class TestOwnTime:
    def _ask(self, flow, created):
        picker = press_label(flow, created, texts.BTN_SCHEDULE, now=NOW)
        ask = press_label(flow, picker, texts.BTN_OWN_TIME, now=NOW)
        assert texts.TIME_ASK in ask.text
        return ask

    def test_past_time_refused_then_future_accepted(self, services):
        flow, _ = build_flow(services)
        created = _draft_created(flow)
        post_id = _post_id(created.text)
        ask = self._ask(flow, created)
        # bare ЧЧ:ММ rolls to the next future occurrence, so a truly past
        # instant needs a full date: today 09:00 MSK < now (11:00 MSK)
        past = run(flow.text(now=NOW, text="21.10.2026 09:00"))
        assert past.text.startswith(texts.TIME_NOT_FUTURE)
        good = run(flow.text(now=NOW, text="15:00"))
        assert good.text.startswith(texts.CONFIRM_SUMMARY_PREFIX)
        press_label(flow, good, texts.BTN_CONFIRM, now=NOW)
        pub = services.publications.get_by_post(post_id)
        moscow = ZoneInfo("Europe/Moscow")
        assert pub.minute_epoch == int(
            datetime(2026, 10, 21, 15, 0, tzinfo=moscow).timestamp()
        )

    def test_full_date_time_format_accepted(self, services):
        flow, _ = build_flow(services)
        created = _draft_created(flow)
        ask = self._ask(flow, created)
        good = run(flow.text(now=NOW, text="22.10.2026 14:30"))
        assert good.text.startswith(texts.CONFIRM_SUMMARY_PREFIX)
        press_label(flow, good, texts.BTN_CONFIRM, now=NOW)
        pub = services.publications.get_by_post(_post_id(created.text))
        moscow = ZoneInfo("Europe/Moscow")
        assert pub.minute_epoch == int(
            datetime(2026, 10, 22, 14, 30, tzinfo=moscow).timestamp()
        )

    def test_garbage_time_explains_format(self, services):
        flow, _ = build_flow(services)
        created = _draft_created(flow)
        self._ask(flow, created)
        bad = run(flow.text(now=NOW, text="завтра после обеда"))
        assert bad.text.startswith(texts.TIME_BAD_INPUT)

    def test_taken_minute_refused_at_confirm_time(self, services):
        flow, _ = build_flow(services)
        created = _draft_created(flow)
        post_id = _post_id(created.text)
        picker = press_label(flow, created, texts.BTN_SCHEDULE, now=NOW)
        confirm = press_label(flow, picker, texts.BTN_NEXT_SLOT, now=NOW)
        # another post wins the same minute between view and confirmation
        rival = services.drafts.create(text_content(), now_epoch=NOW)
        services.publications.schedule(rival, future(minutes=60), now_epoch=NOW)
        refused = press_label(flow, confirm, texts.BTN_CONFIRM, now=NOW)
        assert refused.text.startswith(texts.MINUTE_TAKEN_RU)
        assert texts.PICK_TIME in refused.text
        # the rival owns the slot; our post is still an untouched draft
        assert services.publications.get_by_post(rival) is not None
        assert services.publications.get_by_post(post_id) is None
        assert services.drafts.is_draft(post_id)


class TestRecentSuggestions:
    def test_used_manual_time_is_recorded_and_offered(self, services):
        flow, _ = build_flow(services)
        first = _draft_created(flow)
        picker = press_label(flow, first, texts.BTN_SCHEDULE, now=NOW)
        ask = press_label(flow, picker, texts.BTN_OWN_TIME, now=NOW)
        # record 16:40 via a far date so its minute stays free for the
        # suggestion's natural next occurrence (today 16:40)
        good = run(flow.text(now=NOW, text="25.10.2026 16:40"))
        press_label(flow, good, texts.BTN_CONFIRM, now=NOW)

        second = _draft_created(flow)
        picker2 = press_label(flow, second, texts.BTN_SCHEDULE, now=NOW)
        suggestion = next(l for l in labels(picker2) if l.startswith("16:40"))
        confirm = press_label(flow, picker2, suggestion, now=NOW)
        assert confirm.text.startswith(texts.CONFIRM_SUMMARY_PREFIX)
        scheduled = press_label(flow, confirm, texts.BTN_CONFIRM, now=NOW)
        assert scheduled.text.startswith(texts.SCHEDULED_PREFIX)
        pub = services.publications.get_by_post(_post_id(second.text))
        moscow = ZoneInfo("Europe/Moscow")
        # same clock time, next future occurrence (today 16:40 MSK)
        assert pub.minute_epoch == int(
            datetime(2026, 10, 21, 16, 40, tzinfo=moscow).timestamp()
        )

    def test_manual_times_not_recorded_on_abandoned_confirm(self, services):
        flow, _ = build_flow(services)
        created = _draft_created(flow)
        picker = press_label(flow, created, texts.BTN_SCHEDULE, now=NOW)
        press_label(flow, picker, texts.BTN_OWN_TIME, now=NOW)
        run(flow.text(now=NOW, text="17:15"))
        # no confirmation press: the time must not enter manual history
        assert services.manual_times.recent(limit=5) == []
