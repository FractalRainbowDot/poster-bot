"""Post-publish navigation-edit pass (CHUNK4): links only, bodies immutable.

After every part is confirmed and the publication COMPLETED as published,
the sender edits each received part exactly once: the request text stays
byte-identical to the frozen part, the entity list is the ORIGINAL sent
entities plus appended 'text_link' entities over the linked nav-footer
labels (the part's own label stays plain, a part never links to itself),
and the URLs come only from the sibling parts' durable receipt permalinks.
Targets are own-receipt message ids only (album captions ride the FIRST
item id). A failed or crashed edit never re-sends a body, never parks the
published publication, never propagates, is recorded on navigation_edits
and ends with an owner notice about the incomplete navigation. Nothing is
ever auto-retried.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from conftest import NOW
from fake_transport import FakeTransport
from planned_helpers import (
    base_entities_of,
    gated_sender,
    latest_attempt,
    logical_body,
    nav_rows,
    part_rows,
    planned_publication,
    pub_status,
)

from poster_bot.domain.content import Entity, MediaRef
from poster_bot.domain.planner import LogicalPost
from poster_bot.repo.sender import SenderService
from poster_bot.transport.ports import DefinitiveFailure, UncertainOutcome


def run(coro):
    return asyncio.run(coro)


def three_parts(services, **over):
    return planned_publication(services, logical_body((1600,) * 6), **over)


def expected_nav_entities(part, permalinks: dict[int, str]) -> tuple:
    """Base entities exactly as sent, plus text_links at the LINKED slot
    offsets pointing at sibling receipt permalinks (plan slot order)."""
    links = tuple(
        Entity(type="text_link", offset=slot.utf16_offset,
               length=slot.utf16_length, url=permalinks[slot.part_index])
        for slot in part.footer.slots if slot.linked
    )
    return base_entities_of(part) + links


def permalink_band(n: int) -> dict[int, str]:
    """Part permalinks the FakeTransport defaults hand out for N text parts."""
    return {i: f"https://t.me/c/test/{9000 + i}" for i in range(1, n + 1)}


class TestNavRequests:
    def test_three_text_parts_receive_exact_reciprocal_link_edits(self,
                                                                  services):
        pub, _, plan = three_parts(services)
        transport = FakeTransport()
        run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        links = permalink_band(3)
        assert len(transport.edit_calls) == 3
        for i, request in enumerate(transport.edit_calls):
            part = plan.parts[i]
            assert request.kind == "text"
            assert request.message_id == 9001 + i  # own receipt id, in order
            assert request.text == part.published_text()  # byte-identical
            assert request.entities == expected_nav_entities(part, links)

    def test_own_label_stays_plain_never_links_to_itself(self, services):
        pub, _, plan = three_parts(services)
        transport = FakeTransport()
        run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        for i, request in enumerate(transport.edit_calls):
            part = plan.parts[i]
            own = next(s for s in part.footer.slots if not s.linked)
            assert own.part_index == i + 1
            base = len(base_entities_of(part))
            for ent in request.entities[base:]:  # only appended links
                assert ent.offset != own.utf16_offset
            # exactly one link per sibling part, nothing else appended
            assert len(request.entities) == base + len(plan.parts) - 1

    def test_edits_start_only_after_the_publication_completed(self, services):
        pub, _, _ = three_parts(services)
        seen: list[tuple[str, str]] = []

        def probe(_request) -> None:
            attempt = latest_attempt(services, pub.id)
            seen.append((pub_status(services, pub.id), attempt["state"]))

        transport = FakeTransport()
        transport.on_edit = probe
        run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        assert seen and all(state == ("published", "finished")
                            for state in seen)

    def test_targets_are_only_this_attempt_own_receipt_ids(self, services):
        pub, _, _ = three_parts(services)
        transport = FakeTransport()
        run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        attempt = latest_attempt(services, pub.id)
        rows = part_rows(services, attempt["id"])
        for request, row in zip(transport.edit_calls, rows):
            assert request.message_id in row["ids"]


class TestNavMediaPart:
    def test_album_caption_edit_targets_first_item_id(self, services):
        logical = LogicalPost(body=logical_body((3000, 3000)).body,
                              media=(MediaRef("photo", "p1"),
                                     MediaRef("photo", "p2")))
        pub, _, plan = planned_publication(services, logical)
        assert plan.parts[0].content.kind == "album"
        transport = FakeTransport()
        run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        attempt = latest_attempt(services, pub.id)
        rows = part_rows(services, attempt["id"])
        assert rows[0]["ids"] == [9001, 9002]  # album: one id per item
        album_edit = transport.edit_calls[0]
        assert album_edit.kind == "caption"          # editMessageCaption path
        assert album_edit.message_id == 9001         # caption rides item ONE
        assert album_edit.text == plan.parts[0].published_text()
        # sibling links point at the TEXT parts' own permalinks
        links = {2: "https://t.me/c/test/9003", 3: "https://t.me/c/test/9004"}
        assert album_edit.entities == expected_nav_entities(plan.parts[0],
                                                            links)


class TestNavResults:
    def test_every_part_carries_a_confirmed_nav_edit(self, services):
        pub, _, _ = three_parts(services)
        transport = FakeTransport()
        run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        attempt = latest_attempt(services, pub.id)
        assert [(r["index"], r["state"], r["last_error"])
                for r in nav_rows(services, attempt["id"])] == \
            [(1, "confirmed", None), (2, "confirmed", None),
             (3, "confirmed", None)]

    def test_single_part_plan_needs_no_navigation_edits(self, services):
        pub, _, plan = planned_publication(services, logical_body((300,)))
        assert len(plan.parts) == 1 and plan.parts[0].footer is None
        transport = FakeTransport()
        run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        assert pub_status(services, pub.id) == "published"
        assert transport.edit_calls == []
        assert services.conn.execute(
            "SELECT COUNT(*) c FROM navigation_edits").fetchone()["c"] == 0


class TestNavFailures:
    def test_failed_edit_keeps_published_notifies_and_never_resends_body(
        self, services
    ):
        pub, _, _ = three_parts(services)
        transport = FakeTransport()
        transport.queue_edit(DefinitiveFailure("api_error"))
        results = run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        assert [r.outcome for r in results] == ["published"]
        assert pub_status(services, pub.id) == "published"  # never uncertain
        attempt = latest_attempt(services, pub.id)
        assert [(r["index"], r["state"], r["last_error"])
                for r in nav_rows(services, attempt["id"])] == \
            [(1, "failed", "api_error"), (2, "confirmed", None),
             (3, "confirmed", None)]  # the pass continued over the failure
        assert any("навигаци" in n.lower() and str(pub.id) in n
                   for n in transport.notices)  # incomplete navigation
        assert len(transport.calls) == 3   # bodies were sent exactly once...
        assert len(transport.edit_calls) == 3  # ...and only links were edited
        history = services.conn.execute(
            "SELECT permalink FROM published_history WHERE publication_id=?",
            (pub.id,)).fetchone()
        assert history["permalink"] == "https://t.me/c/test/9001"  # unchanged

    def test_uncertain_edit_is_recorded_and_never_auto_retried(self,
                                                               services):
        pub, _, _ = three_parts(services)
        transport = FakeTransport()
        transport.queue_edit(UncertainOutcome("timeout"))
        run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        attempt = latest_attempt(services, pub.id)
        assert nav_rows(services, attempt["id"])[0] == \
            {"index": 1, "state": "failed", "last_error": "timeout"}
        # a fresh sender on a later tick never re-edites (no auto-retry):
        # the publication is published and only owner action could follow
        fresh = FakeTransport()
        run(gated_sender(services, fresh).send_due(
            now_epoch=pub.scheduled_epoch + 30))
        assert fresh.calls == [] and fresh.edit_calls == []

    def test_adapter_crash_in_nav_phase_records_code_without_leaking(
        self, services, caplog
    ):
        pub, _, _ = three_parts(services)
        sentinel = "PRIVATE-LINK-TEXT-9c22"
        transport = FakeTransport()
        transport.queue_edit(RuntimeError(f"editor died on {sentinel!r}"))
        results = run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))  # must NOT propagate
        assert [r.outcome for r in results] == ["published"]
        assert pub_status(services, pub.id) == "published"
        attempt = latest_attempt(services, pub.id)
        rows = nav_rows(services, attempt["id"])
        assert rows[0]["state"] == "failed"
        assert rows[0]["last_error"] == "transport_error"
        assert [r["state"] for r in rows[1:]] == ["confirmed", "confirmed"]
        assert any("навигаци" in n.lower() for n in transport.notices)
        records = [r for r in caplog.records
                   if r.name == "poster_bot.repo.sender"]
        assert all(sentinel not in r.getMessage() and r.exc_info is None
                   for r in records)