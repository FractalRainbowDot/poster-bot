"""In-process outage handling by the poll loop (PLAN §2/§6 reconnect rules).

The outage here is a run of failed polls inside one live process — the
reconnecting cycle must park every post whose minute began during the
outage (even one still inside its window), persist exactly one downtime
notice with the interval and affected ids, deliver it at-least-once, and
never catch up the missed minute afterwards.
"""
from __future__ import annotations

from conftest import Instant, NOW, text_content
from polling_helpers import advance_healthy, build_harness, run

from poster_bot.db import iso
from poster_bot.domain.states import Status
from poster_bot.polling import PollFailure


def kinds(h):
    return [row["kind"] for row in h.app.journal.entries(limit=50)]


class TestInProcessOutage:
    def _fail_for(self, h, seconds, per=10):
        h.source.advance_per_failure = per
        h.source.script = [PollFailure("timeout")] * (seconds // per)

    def test_same_minute_due_post_is_parked_never_sent(self, tmp_path):
        h = build_harness(tmp_path)
        run(h.loop.run_cycle())                 # healthy poll at NOW
        advance_healthy(h, NOW + 10, step=10)   # last successful poll NOW+10
        post_id = h.app.drafts.create(text_content(), now_epoch=NOW + 10)
        # due at NOW+30: INSIDE the 60s failure run that starts right now;
        # at the reconnect (NOW+70) its window [NOW+30, NOW+90) is still open
        pub = h.app.publications.schedule(post_id, Instant(NOW + 30), now_epoch=NOW + 10)
        self._fail_for(h, 60)
        while h.source.script:
            report = run(h.loop.run_cycle())
            assert not report.ok and not report.ticked
            assert h.transport.calls == []  # outage silences the channel
        # reconnection at NOW+70
        report = run(h.loop.run_cycle())
        assert report.reconnect
        assert report.outage_start == NOW + 10
        assert report.downtime_seconds == 60
        assert report.parked_missed == (pub.id,)
        assert h.app.publications.get(pub.id).status is Status.MISSED
        assert h.transport.calls == []  # parked, never replayed
        # one persisted notice: interval + affected ids, delivered this cycle
        assert h.app.notices.pending() == []
        assert len(h.transport.notices) == 1
        text = h.transport.notices[0]
        assert iso(NOW + 10) in text and iso(NOW + 70) in text
        assert str(pub.id) in text
        assert "outage_detected" in kinds(h)
        # no automatic catch-up ever: healthy cycles leave it parked
        advance_healthy(h, NOW + 3600, step=60)
        assert h.transport.calls == []
        assert h.app.publications.get(pub.id).status is Status.MISSED

    def test_long_outage_notices_even_without_any_parked_work(self, tmp_path):
        h = build_harness(tmp_path)
        run(h.loop.run_cycle())  # last_poll=NOW
        self._fail_for(h, 300)
        while any(isinstance(x, Exception) for x in h.source.script):
            assert not run(h.loop.run_cycle()).ok
        report = run(h.loop.run_cycle())
        assert report.reconnect and report.downtime_seconds == 300
        assert report.parked_missed == () and report.parked_uncertain == ()
        assert report.notice_queued  # gap beyond tolerance alone qualifies
        notice = h.app.notices.pending()  # delivered during the tick
        assert notice == []
        assert len(h.transport.notices) == 1
        text = h.transport.notices[0]
        # intentional localized text (runtime.notice_parts): RU "no affected
        # publications" clause, plus the downtime interval in ISO UTC
        assert "Затронутых публикаций нет." in text
        assert iso(NOW) in text and iso(NOW + 300) in text

    def test_two_outages_produce_two_notices(self, tmp_path):
        h = build_harness(tmp_path)
        run(h.loop.run_cycle())
        self._fail_for(h, 200)
        while any(isinstance(x, Exception) for x in h.source.script):
            run(h.loop.run_cycle())
        first = run(h.loop.run_cycle())
        assert first.notice_queued
        # healthy in between, then a second distinct outage
        advance_healthy(h, first.outage_start + 600, step=60)
        self._fail_for(h, 200)
        while any(isinstance(x, Exception) for x in h.source.script):
            run(h.loop.run_cycle())
        second = run(h.loop.run_cycle())
        assert second.notice_queued
        # both notices reached the owner (at-least-once, distinct intervals)
        assert len(h.transport.notices) == 2
        assert h.app.conn.execute(
            "SELECT COUNT(*) FROM owner_notices WHERE kind='recovery'"
        ).fetchone()[0] == 2

    def test_healthy_gaps_within_tolerance_are_not_outages(self, tmp_path):
        h = build_harness(tmp_path)
        for _ in range(5):
            h.clock.advance(100)  # slow but healthy: gaps under the tolerance
            report = run(h.loop.run_cycle())
            assert report.ok and not report.reconnect
        assert "outage_detected" not in kinds(h)
        assert h.app.notices.pending() == []
        assert h.transport.notices == []

    def test_sending_at_outage_start_becomes_uncertain_on_reconnect(self, tmp_path):
        """A publication claimed but never finished (process effectively
        died mid-send) parks 'uncertain' on the reconnect sweep — with no
        automatic resend."""
        h = build_harness(tmp_path)
        run(h.loop.run_cycle())
        post_id = h.app.drafts.create(text_content(), now_epoch=NOW)
        pub = h.app.publications.schedule(post_id, Instant(NOW + 60), now_epoch=NOW)
        # claimed exactly at its minute, then the loop "hangs" (failure run)
        h.clock.t = pub.scheduled_epoch
        h.app.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch)
        self._fail_for(h, 200)
        while any(isinstance(x, Exception) for x in h.source.script):
            run(h.loop.run_cycle())
        report = run(h.loop.run_cycle())
        assert report.reconnect
        assert report.parked_uncertain == (pub.id,)
        assert h.app.publications.get(pub.id).status is Status.UNCERTAIN
        assert h.transport.calls == []  # nothing is ever replayed
        text = h.transport.notices[0]
        assert str(pub.id) in text and iso(NOW) in text  # interval starts at last health