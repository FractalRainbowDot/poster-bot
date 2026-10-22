"""Owner-flow publication management: pause/resume, content/time edits,
cancel-vs-delete, published links, uncertain resolutions and immutability of
sending/publicated state. The channel transport is never published to."""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import NOW, future, text_content
from ui_flow_helpers import build_flow, find_token, labels, press, press_label, run

from poster_bot.domain.states import Status
from poster_bot.domain.time import Instant
import poster_bot.ui.texts as texts

MOSCOW = ZoneInfo("Europe/Moscow")


def _number(text: str) -> int:
    return int(re.search(r"№(\d+)", text).group(1))


def _draft_created(flow):
    run(flow.new_post(now=NOW))
    staged = run(flow.text(now=NOW, text="управляемый пост"))
    return press_label(flow, staged, texts.BTN_CONTINUE, now=NOW)


def _scheduled(flow, services, *, post_id: int | None = None):
    created = _draft_created(flow)
    picker = press_label(flow, created, texts.BTN_SCHEDULE, now=NOW)
    confirm = press_label(flow, picker, texts.BTN_NEXT_SLOT, now=NOW)
    confirm = press_label(flow, confirm, '12:00', now=NOW)
    press_label(flow, confirm, texts.BTN_CONFIRM, now=NOW)
    return services.publications.get_by_post(_number(created.text))


def _open_pub(flow, pub_id: int, *, now: int = NOW):
    day = flow.plan_today(now=now)
    view = press_label(flow, day, day.rows[0][0].label, now=now)
    assert f"Публикация №{pub_id}." in view.text
    return view


class TestPauseResume:
    def test_pause_then_resume(self, services):
        flow, preview = build_flow(services)
        pub = _scheduled(flow, services)
        view = _open_pub(flow, pub.id)
        assert texts.BTN_PAUSE in labels(view)
        paused = press_label(flow, view, texts.BTN_PAUSE, now=NOW)
        assert texts.STATUS_RU[Status.SUSPENDED] in paused.text
        resumed = press_label(flow, paused, texts.BTN_RESUME, now=NOW)
        assert texts.STATUS_RU[Status.SCHEDULED] in resumed.text
        assert services.publications.get(pub.id).status is Status.SCHEDULED
        assert preview.calls == []

    def test_resume_of_past_time_requires_replan(self, services):
        flow, _ = build_flow(services)
        created = _draft_created(flow)
        post_id = _number(created.text)
        pub = services.publications.schedule(post_id, future(60), now_epoch=NOW)
        services.publications.pause(pub.id, pub.version, now_epoch=NOW)
        view = _open_pub(flow, pub.id, now=NOW + 7200)  # minute has passed
        refused = press_label(flow, view, texts.BTN_RESUME, now=NOW + 7200)
        assert texts.PUB_RESUME_PAST in refused.text
        assert services.publications.get(pub.id).status is Status.SUSPENDED


class TestEdits:
    def test_content_edit_commits_on_continue_and_pauses(self, services):
        flow, _ = build_flow(services)
        pub = _scheduled(flow, services)
        view = _open_pub(flow, pub.id)
        press_label(flow, view, texts.BTN_EDIT_CONTENT, now=NOW)
        staged = run(flow.text(now=NOW, text="новый текст"))
        done = press_label(flow, staged, texts.BTN_CONTINUE, now=NOW)
        assert texts.EDIT_PAUSED_NOTE in done.text
        fresh = services.publications.get(pub.id)
        assert fresh.status is Status.SUSPENDED
        content = services.dispatch.content_for_publication(pub.id)
        assert content.kind == "text" and content.text == "новый текст"

    def test_time_edit_revalidates_and_moves_minute(self, services):
        flow, _ = build_flow(services)
        pub = _scheduled(flow, services)
        view = _open_pub(flow, pub.id)
        press_label(flow, view, texts.BTN_EDIT_TIME, now=NOW)
        confirm = run(flow.text(now=NOW, text="25.10.2026 10:00"))
        confirm = press_label(flow, confirm, 'Продолжить', now=NOW)
        assert confirm.text.startswith(texts.CONFIRM_SUMMARY_PREFIX)
        done = press_label(flow, confirm, texts.BTN_CONFIRM, now=NOW)
        assert texts.TIME_EDITED in done.text
        fresh = services.publications.get(pub.id)
        assert fresh.status is Status.SCHEDULED
        assert fresh.minute_epoch == int(
            datetime(2026, 10, 25, 10, 0, tzinfo=MOSCOW).timestamp()
        )


class TestCancelAndDelete:
    def test_cancel_returns_post_to_drafts_and_frees_minute(self, services):
        flow, _ = build_flow(services)
        pub = _scheduled(flow, services)
        view = _open_pub(flow, pub.id)
        original = services.drafts.get_content(pub.post_id)
        assert texts.BTN_TO_DRAFTS == "В отложенное"
        ask = press_label(flow, view, texts.BTN_TO_DRAFTS, now=NOW)
        assert ask.text.startswith(texts.PUB_CANCEL_ASK)
        assert "без даты публикации" in ask.text
        done = press_label(flow, ask, texts.BTN_YES, now=NOW)
        assert done.text.startswith(texts.TO_DRAFTS_DONE)
        assert services.publications.get_by_post(pub.post_id) is None
        assert services.drafts.is_draft(pub.post_id)
        assert services.drafts.get_content(pub.post_id) == original
        assert "отложенном без даты публикации" in done.text
        # the minute is genuinely free again
        rival = services.drafts.create(text_content(), now_epoch=NOW)
        moved = services.publications.schedule(
            rival, Instant(pub.minute_epoch), now_epoch=NOW)
        assert moved.minute_epoch == pub.minute_epoch

    def test_draft_delete_needs_separate_confirm(self, services):
        flow, _ = build_flow(services)
        created = _draft_created(flow)
        post_id = _number(created.text)
        ask = press_label(flow, created, texts.BTN_DELETE, now=NOW)
        assert ask.text.startswith(texts.DRAFT_DELETE_ASK)
        press_label(flow, ask, texts.BTN_YES, now=NOW)
        assert post_id not in services.drafts.list_drafts()

    def test_publication_post_delete_removes_post_and_links(self, services):
        flow, _ = build_flow(services)
        pub = _scheduled(flow, services)
        view = _open_pub(flow, pub.id)
        ask = press_label(flow, view, texts.BTN_DELETE, now=NOW)
        assert ask.text.startswith(texts.PUB_DELETE_ASK)
        done = press_label(flow, ask, texts.BTN_YES, now=NOW)
        assert done.text.startswith(texts.DELETED)
        assert services.publications.get_by_post(pub.post_id) is None
        assert pub.post_id not in services.drafts.list_drafts()


class TestSendingImmutable:
    def test_sending_publication_offers_no_mutations(self, services):
        flow, _ = build_flow(services)
        pub = _scheduled(flow, services)
        services.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch)
        view = _open_pub(flow, pub.id)
        assert texts.STATUS_RU[Status.SENDING] in view.text
        assert labels(view) == [texts.BTN_BACK]


class TestPublished:
    def _published(self, services, flow):
        pub = _scheduled(flow, services)
        attempt = services.dispatch.claim(pub.id, pub.version,
                                          now_epoch=pub.scheduled_epoch)
        services.dispatch.complete(pub.id, attempt, (55,),
                                   "https://t.me/c/555/55",
                                   now_epoch=pub.scheduled_epoch)
        return services.publications.get(pub.id)

    def test_published_view_shows_link_and_no_time_edits(self, services):
        flow, _ = build_flow(services)
        pub = self._published(services, flow)
        view = _open_pub(flow, pub.id)
        assert texts.STATUS_RU[Status.PUBLISHED] in view.text
        link = next(b for r in view.rows for b in r if b.label == texts.BTN_LINK)
        assert link.url == "https://t.me/c/555/55"
        assert texts.BTN_PAUSE not in labels(view)
        assert texts.BTN_EDIT_TIME not in labels(view)
        # The full-width day row opens the read-only detail containing the link.
        day = flow.plan_today(now=NOW)
        assert len(day.rows[0]) == 1 and " | ✅ | " in day.rows[0][0].label
        detail = press_label(flow, day, day.rows[0][0].label, now=NOW)
        assert any(b.url == "https://t.me/c/555/55" for r in detail.rows for b in r)


class TestUncertain:
    def _uncertain(self, services, flow):
        pub = _scheduled(flow, services)
        attempt = services.dispatch.claim(pub.id, pub.version,
                                          now_epoch=pub.scheduled_epoch)
        services.dispatch.mark_uncertain(pub.id, attempt, "timeout",
                                         now_epoch=pub.scheduled_epoch)
        return services.publications.get(pub.id)

    def test_mark_published_via_entered_message_id(self, services):
        flow, _ = build_flow(services)
        pub = self._uncertain(services, flow)
        view = _open_pub(flow, pub.id)
        assert texts.BTN_MARK_PUBLISHED in labels(view)
        ask = press_label(flow, view, texts.BTN_MARK_PUBLISHED, now=NOW)
        assert ask.text == texts.RESOLUTION_ASK
        bad = run(flow.text(now=NOW, text="не знаю точно"))
        assert bad.text.startswith(texts.RESOLUTION_BAD)
        done = run(flow.text(now=NOW, text="42"))
        assert texts.MARKED_PUBLISHED in done.text
        fresh = services.publications.get(pub.id)
        assert fresh.status is Status.PUBLISHED
        links = services.dispatch.history_links(pub.post_id)
        # private-channel permalink built from the entered id
        assert links[-1]["permalink"] == "https://t.me/c/555/42"

    def test_to_drafts_requires_explicit_risk_ack(self, services):
        flow, _ = build_flow(services)
        pub = self._uncertain(services, flow)
        view = _open_pub(flow, pub.id)
        ask = press_label(flow, view, texts.BTN_TO_DRAFTS, now=NOW)
        assert ask.text == texts.ACK_RISK_TEXT
        assert texts.BTN_ACK_RISK in labels(ask)
        done = press_label(flow, ask, texts.BTN_ACK_RISK, now=NOW)
        assert done.text.startswith(texts.TO_DRAFTS_DONE)
        assert services.publications.get_by_post(pub.post_id) is None
        assert services.drafts.is_draft(pub.post_id)

    def test_ack_replay_after_resolution_is_stale(self, services):
        flow, _ = build_flow(services)
        pub = self._uncertain(services, flow)
        view = _open_pub(flow, pub.id)
        ask = press_label(flow, view, texts.BTN_TO_DRAFTS, now=NOW)
        token = find_token(ask, texts.BTN_ACK_RISK)
        press(flow, token, now=NOW)
        replay = press(flow, token, now=NOW + 1)
        assert replay.text == texts.BUTTON_STALE
