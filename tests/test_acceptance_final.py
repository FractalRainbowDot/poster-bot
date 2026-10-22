"""Independent final-acceptance probes of the owner user contract.

These tests compose the REAL production objects — OwnerFlow over Services,
Runtime over the SAME connection, the real PollLoop through compose_live —
with offline fakes only (FakeTransport / fake API session / fake clock):
no network, no Telegram, no real run, no .env reads.

Deliberately independent of the per-area suites: each probe walks the full
user journey end to end so a wiring break between layers (intake -> draft ->
preview -> schedule -> due send -> history) is caught here even if every
unit-level test still passes. Proven-elsewhere behaviors (same-minute
outage no-catchup, redelivery dedup, minute uniqueness races, timezone
round-trips) are NOT re-implemented; they are cited in the final report.

Not assumed anywhere: exactly-once UI effects. Button tokens are
consume-first by design; a failure after consumption can lose only the
preview while the durable draft must survive (asserted below).
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import NOW, text_content
from fake_transport import FakeTransport
from ui_flow_helpers import build_flow, find_token, press, press_label, run

import poster_bot.ui.texts as texts
from poster_bot.domain.content import Entity
from poster_bot.domain.states import Status
from poster_bot.domain.errors import MinuteTakenError
from poster_bot.domain.time import Instant
from poster_bot.repo.inbox import InboxRepo
from poster_bot.repo.notifications import OwnerNoticeStore
from poster_bot.repo.runtime_state import RuntimeState
from poster_bot.runtime import Runtime
from poster_bot.transport.ports import UncertainOutcome

MOSCOW = ZoneInfo("Europe/Moscow")
DAY = 24 * 3600


def _post_id(reply_text: str) -> int:
    match = re.search(r"№(\d+)", reply_text)
    assert match, reply_text
    return int(match.group(1))


def build_runtime(services, channel: FakeTransport) -> Runtime:
    """Runtime exactly as Runtime.for_app composes it, over the flow's conn."""
    return Runtime(
        dispatch=services.dispatch,
        notices=OwnerNoticeStore(services.conn, services.journal),
        runtime_state=RuntimeState(services.conn),
        transport=channel,
        journal=services.journal,
        inbox=InboxRepo(services.conn),
    )


def _schedule(flow, created, *, now: int = NOW, own_time: str | None = None):
    """Slot or manual-time confirmation; returns the scheduled reply."""
    picker = press_label(flow, created, texts.BTN_SCHEDULE, now=now)
    if own_time is None:
        confirm = press_label(flow, picker, texts.BTN_NEXT_SLOT, now=now)
        hour = next(button.label for row in confirm.rows for button in row
                    if re.fullmatch(r'\d{2}:00', button.label))
        confirm = press_label(flow, confirm, hour, now=now)
    else:
        press_label(flow, picker, texts.BTN_OWN_TIME, now=now)
        confirm = run(flow.text(now=now, text=own_time))
        confirm = press_label(flow, confirm, 'Продолжить', now=now)
        assert confirm.text.startswith(texts.CONFIRM_SUMMARY_PREFIX)
    scheduled = press_label(flow, confirm, texts.BTN_CONFIRM, now=now)
    assert scheduled.text.startswith(texts.SCHEDULED_PREFIX)
    return scheduled


def _journey(services, stage, *, finish=texts.BTN_CONTINUE, own_time=None):
    """Full contract walk for one content kind: intake -> Continue ->
    owner preview -> schedule confirm -> due tick -> channel publish ->
    history link. Returns (flow, preview, channel, post_id, pub)."""
    flow, preview = build_flow(services)
    channel = FakeTransport()
    rt = build_runtime(services, channel)

    run(flow.new_post(now=NOW))
    staged = stage(flow)
    created = press_label(flow, staged, finish, now=NOW)
    assert texts.DRAFT_SAVED in created.text
    assert texts.PREVIEW_SENT in created.text
    post_id = _post_id(created.text)
    content = services.drafts.get_content(post_id)
    # the preview went to the owner path; the channel path stayed untouched
    assert len(preview.owner_previews) == 1
    assert channel.calls == []

    _schedule(flow, created, own_time=own_time)
    pub = services.publications.get_by_post(post_id)
    assert pub.status is Status.SCHEDULED
    # unique future minute: strictly future and minute-aligned
    assert pub.minute_epoch > NOW and pub.minute_epoch % 60 == 0

    early = run(rt.tick(now_epoch=NOW))
    assert early.sent == () and channel.calls == []

    report = run(rt.tick(now_epoch=pub.scheduled_epoch))
    assert [r.outcome for r in report.sent] == ["published"]
    assert services.publications.get(pub.id).status is Status.PUBLISHED
    assert len(channel.calls) == 1
    assert channel.calls[0] == content  # published exactly what was drafted
    links = services.dispatch.history_links(post_id)
    assert len(links) == 1 and links[0]["permalink"].startswith("https://t.me/")
    # nothing ever re-sends afterwards
    report2 = run(rt.tick(now_epoch=pub.scheduled_epoch + 61))
    assert report2.sent == () and len(channel.calls) == 1
    return flow, preview, channel, post_id, pub


class TestFullJourneyPerKind:
    def test_text_journey(self, services):
        _journey(services, lambda f: run(
            f.text(now=NOW, text="финальная проверка")))

    def test_photo_journey(self, services):
        _journey(services, lambda f: run(f.media(
            now=NOW, kind="photo", file_id="ph-final", caption=None,
            media_group_id=None)))

    def test_video_journey(self, services):
        # single video end-to-end was never walked through the full journey
        _journey(services, lambda f: run(f.media(
            now=NOW, kind="video", file_id="vd-final", caption=None,
            media_group_id=None)))

    def test_single_mp3_journey_confirms_manual_time_without_tz_shift(
        self, services
    ):
        # single audio (mp3) intake -> owner preview -> manual time confirm
        # -> due send: never walked end to end before; the manual minute is
        # re-checked to the exact Moscow epoch (the tz must not shift it)
        _, preview, channel, _, pub = _journey(
            services,
            lambda f: run(f.media(now=NOW, kind="audio", file_id="mp3-final",
                                  caption="Трек", media_group_id=None)),
            own_time="25.10.2026 12:30",
        )
        assert pub.minute_epoch == int(
            datetime(2026, 10, 25, 12, 30, tzinfo=MOSCOW).timestamp()
        )
        assert channel.calls[0].kind == "audio"
        assert channel.calls[0].media.caption == "Трек"
        assert preview.calls == []  # previews never publish to the channel

    def test_taken_minute_cannot_be_double_booked(self, services):
        # journey-level uniqueness data point while the reservation is live:
        # a scheduled post's minute is refused to any rival (published/
        # cancelled posts release it — see uq_reserved_minute partial index)
        flow, _ = build_flow(services)
        run(flow.new_post(now=NOW))
        staged = run(flow.text(now=NOW, text="первый занят"))
        created = press_label(flow, staged, texts.BTN_CONTINUE, now=NOW)
        _schedule(flow, created)
        pub = services.publications.get_by_post(_post_id(created.text))
        assert pub.status is Status.SCHEDULED
        rival = services.drafts.create(text_content("соперник"), now_epoch=NOW)
        with pytest.raises(MinuteTakenError):
            services.publications.schedule(
                rival, Instant(pub.minute_epoch), now_epoch=NOW)


class TestAlbumAndForwardCarryCaptions:
    def test_album_captions_entities_survive_preview_and_send(self, services):
        def stage(f):
            run(f.media(now=NOW, kind="photo", file_id="p1", caption="первая",
                        caption_entities=(Entity("bold", 0, 6),),
                        media_group_id="g1"))
            return run(f.media(now=NOW, kind="video", file_id="v1",
                               caption="вторая", media_group_id="g1"))

        _, preview, channel, post_id, pub = _journey(
            services, stage, own_time="26.10.2026 09:15")
        # intake hop: per-item captions/entities stored, no merged caption
        content = services.drafts.get_content(post_id)
        assert content.kind == "album" and content.caption is None
        assert [i.caption for i in content.items] == ["первая", "вторая"]
        assert content.items[0].caption_entities == (Entity("bold", 0, 6),)
        # preview hop: the owner copy carries the same per-item data
        shown = preview.owner_previews[0]
        assert shown.kind == "album"
        assert [i.caption for i in shown.items] == ["первая", "вторая"]
        assert shown.items[0].caption_entities == (Entity("bold", 0, 6),)
        # send hop: the channel received identical items (the journey pinned
        # published == stored; the item-level facts restated explicitly)
        assert [i.caption for i in channel.calls[0].items] == ["первая",
                                                               "вторая"]
        assert channel.calls[0].items[0].caption_entities == \
            (Entity("bold", 0, 6),)
        # the manually confirmed minute survived every hop without drift
        assert pub.minute_epoch == int(
            datetime(2026, 10, 26, 9, 15, tzinfo=MOSCOW).timestamp())

    def test_forward_copy_preserves_item_captions_into_preview(self, services):
        # the copy hop (as-is forward -> editable copy) is pinned in the
        # collection suite for the DRAFT; this adds the PREVIEW hop with an
        # entity the collection fixture never carried
        from poster_bot.ui.staged import Staged

        flow, preview = build_flow(services)
        run(flow.new_post(now=NOW))
        first = Staged()
        first.add_media("photo", "p1", "cap1", (Entity("italic", 0, 4),),
                        "g1")
        run(flow.forwarded(now=NOW, src_chat_id=-100999,
                           src_message_id=42, media_group_id="g1",
                           copy_candidate=first))
        # Each forwarded album message contributes its own single-item
        # candidate and distinct source ID, just as real intake does.
        second = Staged()
        second.add_media("video", "v1", "cap2", (), "g1")
        ask = run(flow.forwarded(now=NOW, src_chat_id=-100999,
                                 src_message_id=43, media_group_id="g1",
                                 copy_candidate=second))
        copied = press_label(flow, ask, texts.BTN_MAKE_COPY, now=NOW)
        created = press_label(flow, copied, texts.BTN_CONTINUE, now=NOW)
        content = services.drafts.get_content(_post_id(created.text))
        assert content.kind == "album"
        assert [i.caption for i in content.items] == ["cap1", "cap2"]
        shown = preview.owner_previews[0]
        assert [i.caption for i in shown.items] == ["cap1", "cap2"]
        assert shown.items[0].caption_entities == (Entity("italic", 0, 4),)

    def test_forward_as_is_journey_publishes_source_reference(self, services):
        from poster_bot.ui.staged import copy_candidate_from_media

        def stage(f):
            return run(f.forwarded(now=NOW, src_chat_id=-100999,
                                   src_message_id=42, media_group_id=None,
                                   copy_candidate=copy_candidate_from_media(
                                       "photo", "ph-src", None, (), None)))

        _, _, channel, _, _ = _journey(services, stage,
                                       finish=texts.BTN_FORWARD_AS_IS)
        # the channel call carried the source reference, not a re-typed post
        assert channel.calls[0].kind == "forward"
        assert channel.calls[0].forward.chat_id == -100999
        assert channel.calls[0].forward.message_ids == (42,)


class TestEditPauseNoSend:
    """Required contract: content-edit commits pause the publication and the
    due minute must silently pass with zero channel traffic. The claim
    filter excludes SUSPENDED by construction — this pins that at the
    behavior level through the real Runtime tick."""

    def test_paused_publication_untouched_by_due_ticks(self, services):
        flow, preview = build_flow(services)
        channel = FakeTransport()
        rt = build_runtime(services, channel)
        run(flow.new_post(now=NOW))
        staged = run(flow.text(now=NOW, text="до правки"))
        created = press_label(flow, staged, texts.BTN_CONTINUE, now=NOW)
        _schedule(flow, created)
        pub = services.publications.get_by_post(_post_id(created.text))
        day = flow.plan_today(now=NOW)
        view = press_label(flow, day, " | 🕒 | до правки", now=NOW)
        press_label(flow, view, texts.BTN_EDIT_CONTENT, now=NOW)
        staged2 = run(flow.text(now=NOW, text="после правки"))
        done = press_label(flow, staged2, texts.BTN_CONTINUE, now=NOW)
        assert texts.EDIT_PAUSED_NOTE in done.text
        assert services.publications.get(pub.id).status is Status.SUSPENDED
        # the minute arrives while paused: nothing may go out
        run(rt.tick(now_epoch=pub.scheduled_epoch))
        assert channel.calls == []
        # window fully closed: still nothing; the pause stays untouched
        run(rt.tick(now_epoch=pub.scheduled_epoch + 61))
        assert channel.calls == []
        fresh = services.publications.get(pub.id)
        assert fresh.status is Status.SUSPENDED
        # and it is the EDITED content that waits, verbatim
        assert services.dispatch.content_for_publication(
            pub.id).text == "после правки"
        assert preview.calls == [] and channel.calls == []


class TestConsumeFirstPreviewLoss:
    """Pins the documented (not invented) semantics: token consumption
    precedes the preview call, a failed preview loses ONLY the preview —
    the durable draft remains, the spent token is permanently stale (no
    exactly-once redelivery of the effect), retry works from the draft."""

    def test_failed_preview_keeps_draft_token_spent_retry_ok(self, services):
        flow, preview = build_flow(services)
        preview.preview_script = [UncertainOutcome("timeout")]
        run(flow.new_post(now=NOW))
        staged = run(flow.text(now=NOW, text="не потерян"))
        token = find_token(staged, texts.BTN_CONTINUE)
        done = press(flow, token, now=NOW)
        assert texts.DRAFT_SAVED in done.text
        assert texts.PREVIEW_FAILED_PREFIX in done.text
        assert texts.PREVIEW_RETRY_HINT in done.text
        post_ids = services.drafts.list_drafts()
        assert len(post_ids) == 1  # the draft committed before the failure
        assert services.drafts.get_content(post_ids[0]).text == "не потерян"
        stale = press(flow, token, now=NOW + 5)
        assert stale.text == texts.BUTTON_STALE
        assert services.drafts.list_drafts() == post_ids  # still exactly one
        retried = press_label(flow, done, texts.BTN_PREVIEW, now=NOW + 10)
        assert texts.PREVIEW_SENT in retried.text
        # the fake transport records attempts, not deliveries: both calls
        # are visible, only the second one reached the owner chat
        assert len(preview.owner_previews) == 2


class TestJournalPruningThroughLiveLoop:
    """Pruning is pinned at the tick unit level; this pins it through the
    PRODUCTION wiring: compose_live composes Runtime.for_app (journal and
    inbox injected) under the real PollLoop, so a healthy live cycle prunes
    expired rows at the injected runtime clock. Fully offline: stub bot,
    canned responses, fake clock."""

    def test_healthy_live_cycle_prunes_expired_journal_rows(self, tmp_path):
        from poster_bot.journal import RETENTION_DAYS
        from poster_bot.live import compose_live
        from test_live_composition import LiveApiSession, live_config, make_bot

        class _Clock:
            def __init__(self) -> None:
                self.t = NOW

            def __call__(self) -> int:
                return self.t

        clock = _Clock()
        session = LiveApiSession()
        live = run(compose_live(live_config(tmp_path), clock=clock,
                                bot=make_bot(session), now_epoch=NOW))
        try:
            live.app.journal.record(
                "draft_created", now_epoch=NOW - (RETENTION_DAYS + 1) * DAY)
            live.app.journal.record("scheduled", now_epoch=NOW - 3600,
                                    minute_epoch=NOW - 3600)
            report = run(live.loop.run_cycle())
            assert report.ok
            cutoff = NOW - RETENTION_DAYS * DAY
            assert live.app.conn.execute(
                "SELECT COUNT(*) FROM journal_events WHERE ts_epoch < ?",
                (cutoff,)).fetchone()[0] == 0
            kinds = [r["kind"] for r in live.app.journal.entries(limit=50)]
            assert "journal_pruned" in kinds
            assert "scheduled" in kinds  # the fresh event survives pruning
            # a real getUpdates round went through the durable inbox...
            assert session.poll_calls
            # ...and nothing ever reached the channel during the cycle
            assert session.sent_to_channel() == []
        finally:
            run(live.close())
