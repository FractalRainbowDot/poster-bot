"""Gated PLANNED send path (CHUNK4): the frozen plan is the only send source.

Storage-level truth over the real services fixture: parts arrive at the
transport strictly in plan order, byte-identical to the frozen snapshot;
every ack is persisted as a durable receipt BEFORE the next part is sent;
a broken stack never continues and never resends; the ambient signature
config and posts.content_json can never reach the wire; planless due
publications are refused while gated (kept scheduled, owner prompted once
per process); a forged plan is refused by the claim itself with no attempt
row left behind.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
from conftest import NOW, Instant, text_content
from fake_transport import FakeTransport
from planned_helpers import (
    SIG,
    SIG_OTHER,
    gated_sender,
    latest_attempt,
    logical_body,
    part_rows,
    planned_publication,
    pub_status,
)

from poster_bot.domain.content import Content, ForwardSource, MediaRef
from poster_bot.domain.errors import ContentValidationError
from poster_bot.domain.planner import LogicalPost
from poster_bot.repo.sender import SenderService
from poster_bot.transport.ports import (
    DefinitiveFailure,
    Sent,
    UncertainOutcome,
)


def run(coro):
    return asyncio.run(coro)


def three_parts(services, **over):
    """6 paragraphs x 1600 chars: exactly three text parts under SIG."""
    return planned_publication(services, logical_body((1600,) * 6), **over)


def sent_part_ids(n: int) -> tuple[int, ...]:
    """FakeTransport default single-message ids for parts 1..n."""
    return tuple(range(9001, 9001 + n))


class ObservingTransport(FakeTransport):
    """Calls ``probe(call_number)`` before each publish so tests can read
    the durable receipt state WHILE the stack is mid-flight."""

    def __init__(self, probe) -> None:
        super().__init__()
        self._probe = probe

    async def publish(self, content: Content) -> Sent:
        self._probe(len(self.calls) + 1)
        return await super().publish(content)


class TestStrictOrder:
    def test_parts_sent_in_plan_order_byte_identical(self, services):
        pub, _, plan = three_parts(services)
        transport = FakeTransport()
        results = run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        assert [r.outcome for r in results] == ["published"]
        assert [c.to_json() for c in transport.calls] == \
            [p.content.to_json() for p in plan.parts]

    def test_intent_exists_before_first_call_and_ack_before_next(self, services):
        pub, _, _ = three_parts(services)
        seen: dict[int, list[dict]] = {}

        def probe(call_no: int) -> None:
            attempt = latest_attempt(services, pub.id)
            assert attempt is not None, "intent must precede the first call"
            seen[call_no] = part_rows(services, attempt["id"])

        transport = ObservingTransport(probe)
        run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        assert sorted(seen) == [1, 2, 3]
        # at call 1: all three intents durable, none acked
        assert [r["state"] for r in seen[1]] == ["pending"] * 3
        # at call 2: part 1's receipt was persisted BEFORE this call started
        assert [r["state"] for r in seen[2]] == ["confirmed", "pending",
                                                 "pending"]
        assert seen[2][0]["ids"] == [9001] and seen[2][0]["permalink"]
        # at call 3: part 2 acked durably before part 3 was sent
        assert [r["state"] for r in seen[3]] == ["confirmed", "confirmed",
                                                 "pending"]

    def test_frozen_plan_beats_tampered_posts_content(self, services):
        pub, post_id, plan = three_parts(services)
        forged = text_content("FORGED-SENT-TEXT-e9d1 must never be sent")
        with services.conn:
            services.conn.execute(
                "UPDATE posts SET content_json=? WHERE id=?",
                (forged.to_json(), post_id))
        transport = FakeTransport()
        run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        assert [c.to_json() for c in transport.calls] == \
            [p.content.to_json() for p in plan.parts]
        assert all("FORGED" not in c.text for c in transport.calls
                   if c.kind == "text")

    def test_ambient_signature_change_never_reaches_the_wire(self, services):
        pub, _, plan = three_parts(services, signature=SIG)
        transport = FakeTransport()
        # the ambient config moved to another signature AFTER planning;
        # no ambient replan happens anywhere in the sender path
        run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        for part, sent in zip(plan.parts, transport.calls):
            assert part.published_text() in sent.text
            assert SIG.rendered() in sent.text
            assert SIG_OTHER.rendered() not in sent.text


class TestReceiptsAndCompletion:
    def test_all_receipts_then_history_union_with_part1_permalink(self,
                                                                  services):
        pub, _, _ = three_parts(services)
        transport = FakeTransport()
        results = run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        attempt = latest_attempt(services, pub.id)
        assert (attempt["state"], attempt["outcome"]) == ("finished",
                                                          "published")
        rows = part_rows(services, attempt["id"])
        assert [r["state"] for r in rows] == ["confirmed"] * 3
        assert [r["ids"] for r in rows] == [[i] for i in sent_part_ids(3)]
        history = services.conn.execute(
            "SELECT channel_message_ids, permalink FROM published_history"
            " WHERE publication_id=?", (pub.id,)).fetchone()
        assert json.loads(history["channel_message_ids"]) == \
            list(sent_part_ids(3))  # union of every part receipt
        assert history["permalink"] == rows[0]["permalink"]  # canonical part 1
        assert pub_status(services, pub.id) == "published"
        assert sorted(results[0].message_ids) == list(sent_part_ids(3))


class TestFailureTaxonomy:
    def test_part1_definitive_zero_confirmed_parks_missed(self, services):
        pub, _, _ = three_parts(services)
        transport = FakeTransport()
        transport.queue(DefinitiveFailure("api_error"))
        results = run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        assert [(r.outcome, r.code) for r in results] == \
            [("missed", "api_error")]
        assert pub_status(services, pub.id) == "missed"
        attempt = latest_attempt(services, pub.id)
        assert (attempt["state"], attempt["outcome"]) == \
            ("finished", "definitive_failure")
        # not-attempted parts get a durable terminal outcome too: a pending
        # row beside a resolved attempt means "unknown", which this is not
        assert [r["state"] for r in part_rows(services, attempt["id"])] == \
            ["failed", "failed", "failed"]
        assert len(transport.calls) == 1
        assert services.conn.execute(
            "SELECT COUNT(*) c FROM published_history").fetchone()["c"] == 0

    def test_mid_stack_definitive_after_a_confirmation_is_uncertain(self,
                                                                    services):
        pub, _, _ = three_parts(services)
        transport = FakeTransport()
        transport.queue(
            Sent(message_ids=(9001,), permalink="https://t.me/c/test/9001"),
            DefinitiveFailure("api_error"),
        )
        results = run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        assert [(r.outcome, r.code) for r in results] == \
            [("uncertain", "api_error")]
        assert pub_status(services, pub.id) == "uncertain"
        assert latest_attempt(services, pub.id)["state"] == "abandoned"
        attempt = latest_attempt(services, pub.id)
        assert [r["state"] for r in part_rows(services, attempt["id"])] == \
            ["confirmed", "uncertain", "failed"]
        assert len(transport.calls) == 2  # the stack stopped: no part 3
        # the owner is told about the stopped publication (ids only, never
        # body text) so they can verify the channel manually
        assert any(str(pub.id) in n for n in transport.notices)
        assert all("aaaa" not in n for n in transport.notices)

    def test_part1_uncertain_zero_confirmed_parks_uncertain(self, services):
        pub, _, _ = three_parts(services)
        transport = FakeTransport()
        transport.queue(UncertainOutcome("timeout"))
        results = run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        assert [(r.outcome, r.code) for r in results] == \
            [("uncertain", "timeout")]
        assert pub_status(services, pub.id) == "uncertain"
        attempt = latest_attempt(services, pub.id)
        assert [r["state"] for r in part_rows(services, attempt["id"])] == \
            ["uncertain", "failed", "failed"]

    def test_partial_album_uncertain_stops_the_stack(self, services):
        logical = LogicalPost(body=logical_body((3000, 3000)).body,
                              media=(MediaRef("photo", "p1"),
                                     MediaRef("photo", "p2")))
        pub, _, plan = planned_publication(services, logical)
        assert plan.parts[0].content.kind == "album"
        transport = FakeTransport()
        transport.queue(UncertainOutcome("partial_result"))
        results = run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        assert [(r.outcome, r.code) for r in results] == \
            [("uncertain", "partial_result")]
        assert len(transport.calls) == 1  # the text part was never attempted
        attempt = latest_attempt(services, pub.id)
        assert [r["state"] for r in part_rows(services, attempt["id"])] == \
            ["uncertain", "failed", "failed"]
        assert pub_status(services, pub.id) == "uncertain"

    def test_adapter_crash_mid_stack_resolves_durable_then_propagates(
        self, services, caplog
    ):
        pub, _, _ = three_parts(services)
        transport = FakeTransport()
        sentinel = "PRIVATE-BODY-TEXT 123:TOKEN-SECRET"
        transport.queue(
            Sent(message_ids=(9001,), permalink="https://t.me/c/test/9001"),
            RuntimeError(f"adapter died sending {sentinel!r}"),
        )
        sender = gated_sender(services, transport)
        with caplog.at_level(logging.DEBUG, logger="poster_bot.repo.sender"):
            with pytest.raises(RuntimeError):
                run(sender.send_due(now_epoch=pub.scheduled_epoch))
        assert pub_status(services, pub.id) == "uncertain"
        attempt = latest_attempt(services, pub.id)
        assert attempt["state"] == "abandoned"
        assert [r["state"] for r in part_rows(services, attempt["id"])] == \
            ["confirmed", "uncertain", "failed"]  # crashed part is unknown
        assert any(str(pub.id) in n for n in transport.notices)
        # log hygiene parity with the legacy path: class name only
        records = [r for r in caplog.records
                   if r.name == "poster_bot.repo.sender"]
        assert records and all(sentinel not in r.getMessage()
                               and r.exc_info is None for r in records)
        assert any("RuntimeError" in r.getMessage() for r in records)


class TestRepeatedTicks:
    def test_published_stack_is_never_sent_again(self, services):
        pub, _, plan = three_parts(services)
        transport = FakeTransport()
        sender = gated_sender(services, transport)
        run(sender.send_due(now_epoch=pub.scheduled_epoch))
        run(sender.send_due(now_epoch=pub.scheduled_epoch))  # same minute
        assert len(transport.calls) == len(plan.parts)

    def test_uncertain_stack_is_neither_resumed_nor_resent(self, services):
        pub, _, _ = three_parts(services)
        transport = FakeTransport()
        transport.queue(
            Sent(message_ids=(9001,), permalink="https://t.me/c/test/9001"),
            UncertainOutcome("timeout"),
        )
        sender = gated_sender(services, transport)
        run(sender.send_due(now_epoch=pub.scheduled_epoch))
        run(sender.send_due(now_epoch=pub.scheduled_epoch))
        assert len(transport.calls) == 2  # no auto-resume, no catch-up


class TestPlanlessGatedRefusal:
    @pytest.mark.parametrize("content", [
        text_content("legacy planless body"),
        Content(kind="forward", mode="forward",
                forward=ForwardSource(chat_id=-100555, message_ids=(42,))),
    ], ids=["planless-text", "planless-forward"])
    def test_never_published_unsigned_kept_scheduled_prompt_once(
        self, services, content
    ):
        post_id = services.drafts.create(content, now_epoch=NOW)
        pub = services.publications.schedule(
            post_id, Instant(NOW + 7200), now_epoch=NOW)
        transport = FakeTransport()
        sender = gated_sender(services, transport)
        run(sender.send_due(now_epoch=pub.scheduled_epoch))
        # nothing was claimed, nothing was sent, the schedule stays intact
        assert transport.calls == [] and transport.edit_calls == []
        assert services.conn.execute(
            "SELECT COUNT(*) c FROM send_attempts").fetchone()["c"] == 0
        assert pub_status(services, pub.id) == "scheduled"
        # the owner is prompted exactly once per process to re-compose
        first = list(transport.notices)
        assert any("Откройте предпросмотр" in n and str(pub.id) in n
                   for n in first)
        run(sender.send_due(now_epoch=pub.scheduled_epoch))  # next tick
        assert transport.notices == first  # not re-prompted
        assert transport.calls == []
        assert pub_status(services, pub.id) == "scheduled"


class TestForgedPlanDefense:
    def test_forged_forward_part_refused_at_claim_no_attempt_row(self,
                                                                 services):
        pub, post_id, plan = three_parts(services)
        # forge the stored snapshot: a forward part CANNOT carry the
        # mandatory signature, so strict re-parsing must refuse the claim
        raw = json.loads(plan.to_json())
        raw["parts"][0] = {
            "index": 1, "body_slice": "",
            "content_json": Content(
                kind="forward", mode="forward",
                forward=ForwardSource(chat_id=-100555,
                                      message_ids=(11,))).to_json(),
            "footer": None,
        }
        with services.conn:
            services.conn.execute(
                "UPDATE post_plans SET plan_json=? WHERE post_id=?",
                (json.dumps(raw, ensure_ascii=False), post_id))
        # claim refuses inside its transaction: no attempt row survives
        with pytest.raises(ContentValidationError):
            services.dispatch.claim(pub.id, pub.version,
                                    now_epoch=pub.scheduled_epoch)
        assert services.conn.execute(
            "SELECT COUNT(*) c FROM send_attempts WHERE publication_id=?",
            (pub.id,)).fetchone()["c"] == 0
        assert pub_status(services, pub.id) == "scheduled"
        # the sender surfaces the refusal loudly: a forged plan can neither
        # send nor silently stall the tick
        transport = FakeTransport()
        with pytest.raises(ContentValidationError):
            run(gated_sender(services, transport).send_due(
                now_epoch=pub.scheduled_epoch))
        assert transport.calls == []
        assert pub_status(services, pub.id) == "scheduled"