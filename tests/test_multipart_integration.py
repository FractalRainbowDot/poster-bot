"""CHUNK4 end-to-end: Runtime tick -> planned multipart send -> durable truth.

Runtime-driven integration over the real services fixture: the published
history carries the union of all part receipts with the canonical part-1
permalink, journal rows never carry part text, a hard crash mid-stack keeps
durable unknown-part evidence (never resumed, never replayed), the claim
snapshot survives byte-identically through send, and the planless gated
refusal plus mid-stack uncertainty behave the same through the tick as the
sender-level tests pin.
"""
from __future__ import annotations

import asyncio
import json

from conftest import Instant, NOW, Services, text_content
from fake_transport import FakeTransport
from planned_helpers import (
    latest_attempt,
    logical_body,
    part_rows,
    planned_publication,
    pub_status,
)

from poster_bot.repo.plans import PlanStore
from poster_bot.repo.notifications import OwnerNoticeStore
from poster_bot.repo.receipts import PartReceiptStore
from poster_bot.repo.runtime_state import RuntimeState
from poster_bot.runtime import Runtime
from poster_bot.transport.ports import Sent, UncertainOutcome


def run(coro):
    return asyncio.run(coro)


def gated_runtime(services: Services, transport) -> Runtime:
    """The gated live wiring around the same conn, as Runtime composes it."""
    return Runtime(
        dispatch=services.dispatch,
        notices=OwnerNoticeStore(services.conn, services.journal),
        runtime_state=RuntimeState(services.conn),
        transport=transport,
        plans=PlanStore(services.conn),
        receipts=PartReceiptStore(services.conn),
    )


class TestPublishedStackThroughRuntime:
    def test_history_is_union_of_receipts_with_part1_permalink(self, services):
        pub, _, plan = planned_publication(
            services, logical_body((1600,) * 6))
        transport = FakeTransport()
        report = run(gated_runtime(services, transport).tick(
            now_epoch=pub.scheduled_epoch))
        assert [(r.publication_id, r.outcome) for r in report.sent] == \
            [(pub.id, "published")]
        ids = list(range(9001, 9001 + len(plan.parts)))
        assert sorted(report.sent[0].message_ids) == ids
        history = services.conn.execute(
            "SELECT channel_message_ids, permalink FROM published_history"
            " WHERE publication_id=?", (pub.id,)).fetchone()
        assert json.loads(history["channel_message_ids"]) == ids
        assert history["permalink"] == "https://t.me/c/test/9001"

    def test_journal_carries_no_part_text(self, services):
        pub, _, _ = planned_publication(services, logical_body((1600,) * 6))
        transport = FakeTransport()
        run(gated_runtime(services, transport).tick(
            now_epoch=pub.scheduled_epoch))
        rows = services.conn.execute(
            "SELECT kind, detail FROM journal_events").fetchall()
        assert rows, "the send path journals its steps"
        for row in rows:
            assert "aaaa" not in row["detail"]
            assert "TTTT" not in row["detail"]

    def test_claim_snapshot_survives_byte_identical_to_the_plan(self, services):
        pub, _, plan = planned_publication(services, logical_body((1600,) * 6))
        transport = FakeTransport()
        run(gated_runtime(services, transport).tick(
            now_epoch=pub.scheduled_epoch))
        attempt = latest_attempt(services, pub.id)
        rows = PartReceiptStore(services.conn).parts_of_attempt(attempt["id"])
        # the durable intent IS the frozen plan part, structurally equal...
        assert [r.snapshot for r in rows] == list(plan.parts)
        # ...and what actually reached the transport matches byte-for-byte,
        # including the footer+signature published form (not the logical body)
        assert [c.to_json() for c in transport.calls] == \
            [p.content.to_json() for p in plan.parts]
        for sent_content, part in zip(transport.calls, plan.parts):
            assert part.published_text() == sent_content.text


class TestHardCrashEvidence:
    def test_mid_stack_crash_leaves_unknown_parts_never_resumed(self, services):
        pub, _, _ = planned_publication(services, logical_body((1600,) * 6))
        transport = FakeTransport()
        runtime = gated_runtime(services, transport)
        receipts = PartReceiptStore(services.conn)
        # simulate a hard crash after the first ack: claim (which snapshots
        # the intents) + one recorded receipt, then the process dies
        attempt_id = services.dispatch.claim(pub.id, pub.version,
                                             now_epoch=pub.scheduled_epoch)
        services.dispatch.mark_in_progress(attempt_id,
                                           now_epoch=pub.scheduled_epoch)
        receipts.record_confirmed(attempt_id, 1, (9001,),
                                  "https://t.me/c/test/9001",
                                  now_epoch=pub.scheduled_epoch)
        result = services.dispatch.recovery_sweep(
            now_epoch=pub.scheduled_epoch + 120)
        assert result.uncertain_publication_ids == (pub.id,)
        assert pub_status(services, pub.id) == "uncertain"
        assert [r["state"] for r in part_rows(services, attempt_id)] == \
            ["confirmed", "pending", "pending"]  # durable unknown evidence
        # a fresh tick never resumes or replays the abandoned attempt
        report = run(runtime.tick(now_epoch=pub.scheduled_epoch + 120))
        assert report.sent == ()
        assert transport.calls == []
        assert [r["state"] for r in part_rows(services, attempt_id)] == \
            ["confirmed", "pending", "pending"]
        assert pub_status(services, pub.id) == "uncertain"


class TestPlanlessThroughRuntime:
    def test_planless_due_stays_scheduled_and_owner_prompted_once(self,
                                                                  services):
        post_id = services.drafts.create(text_content("legacy planless"),
                                         now_epoch=NOW)
        pub = services.publications.schedule(post_id, Instant(NOW + 7200),
                                             now_epoch=NOW)
        transport = FakeTransport()
        runtime = gated_runtime(services, transport)
        report = run(runtime.tick(now_epoch=NOW + 7200))
        assert report.sent == ()
        assert transport.calls == []
        assert pub_status(services, pub.id) == "scheduled"
        assert any("Откройте предпросмотр" in n and str(pub.id) in n
                   for n in transport.notices)
        first = list(transport.notices)
        run(runtime.tick(now_epoch=NOW + 7255))  # later minute, same process
        assert transport.notices == first  # not re-prompted
        assert pub_status(services, pub.id) == "scheduled"


class TestMidStackUncertaintyThroughRuntime:
    def test_stopped_stack_is_terminal_after_one_notice(self, services):
        pub, _, _ = planned_publication(services, logical_body((1600,) * 6))
        transport = FakeTransport()
        transport.queue(
            Sent(message_ids=(9001,), permalink="https://t.me/c/test/9001"),
            UncertainOutcome("timeout"),
        )
        runtime = gated_runtime(services, transport)
        report = run(runtime.tick(now_epoch=pub.scheduled_epoch))
        assert report.sent[0].outcome == "uncertain"
        assert report.sent[0].code == "timeout"
        assert pub_status(services, pub.id) == "uncertain"
        assert len(transport.calls) == 2
        assert any(str(pub.id) in n for n in transport.notices)
        run(runtime.tick(now_epoch=pub.scheduled_epoch + 30))
        assert len(transport.calls) == 2  # terminal: no auto-retry at all