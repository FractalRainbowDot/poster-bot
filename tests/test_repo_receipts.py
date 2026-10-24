"""CHUNK2 storage contract: per-part send intents, receipts and navigation.

Encoded decisions (approved brief):
- a planned publication snapshots one intent row per part IN THE CLAIM
  TRANSACTION itself: the intent is durable before any channel call; a
  tampered plan aborts the claim atomically (no half-claimed publication);
- each ack becomes a receipt immediately: ids stored verbatim (the whole
  album list survives) plus the part's permalink; replaying the identical
  ack is idempotent, replaying a DIFFERENT one is refused — a recorded
  receipt can never change or move to another part/attempt;
- uncertain/failed parts are recorded on the part row before the
  publication-level mark_uncertain/fail_definitive (the attempt must still
  be in flight); a late ack cannot overwrite an uncertain part — the
  outcome stays uncertain for manual owner verification;
- publication completion keeps the OLD published_history contract (union
  of every part's ids, one canonical permalink) but refuses while any part
  lacks a receipt, refuses ids that do not match the recorded union and
  refuses a permalink no part actually received — the history can never
  point at messages the durable receipts do not confirm;
- navigation edits are their own durable attempts, only after EVERY part
  of the attempt has a receipt AND the publication completed; a failed
  edit never touches the receipt rows (published bodies stay immutable).
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from conftest import NOW, future, text_content
from poster_bot.db import iso
from poster_bot.domain.errors import (
    AttemptNotFound,
    ContentValidationError,
    TransitionError,
)
from poster_bot.domain.planner import LogicalPost, plan_post
from poster_bot.domain.signature import SignatureConfig
from poster_bot.repo.plans import PlanStore
from poster_bot.repo.receipts import (
    PartNotFound,
    PartReceiptStore,
)

SIG = SignatureConfig(
    main_text="@channel_main",
    main_url="https://t.me/channel_main",
    partner_text="@channel_partner",
    partner_url="https://t.me/channel_partner",
)
NL2 = "\n\n"
CLAIM_AT = NOW + 7200  # inside the window of future() == NOW + 7200


def plan_for(logical: LogicalPost):
    result = plan_post(logical, SIG)
    assert result.plan is not None, result.diagnostics
    return result.plan


def three_part_logical() -> LogicalPost:
    return LogicalPost(body="A" * 3000 + NL2 + "B" * 3000 + NL2
                       + "C" * 3000)


class Scenario:
    """draft -> plan saved -> scheduled; claim() finishes the setup."""

    def __init__(self, services, logical: LogicalPost | None = None):
        self.services = services
        self.receipts = PartReceiptStore(services.conn)
        self.logical = logical or three_part_logical()
        self.plan = plan_for(self.logical)
        self.post_id = services.drafts.create(self.plan.parts[0].content,
                                              now_epoch=NOW)
        PlanStore(services.conn).save(self.post_id, self.logical, self.plan,
                                      now_epoch=NOW)
        self.pub = services.publications.schedule(self.post_id, future(),
                                                  now_epoch=NOW)
        self.attempt: int | None = None

    def claim(self, *, now_epoch: int = CLAIM_AT) -> int:
        self.attempt = self.services.dispatch.claim(self.pub.id,
                                                    self.pub.version,
                                                    now_epoch=now_epoch)
        self.pub = self.services.publications.get(self.pub.id)
        return self.attempt

    def confirm(self, part_index: int, ids: tuple[int, ...],
                *, now_epoch: int = CLAIM_AT + 5):
        permalink = f"https://t.me/c/777/{ids[0]}"
        return self.receipts.record_confirmed(self.attempt, part_index, ids,
                                              permalink,
                                              now_epoch=now_epoch)

    def publish(self, *, start_id: int = 9000):
        """Confirm every part and complete the publication."""
        parts = self.receipts.parts_of_attempt(self.attempt)
        all_ids: list[int] = []
        for i, part in enumerate(parts):
            ids = (start_id + 10 * i + 1,)
            all_ids.extend(ids)
            self.confirm(part.part_index, ids)
        first_permalink = f"https://t.me/c/777/{start_id + 1}"
        self.services.dispatch.complete(self.pub.id, self.attempt,
                                        tuple(all_ids), first_permalink,
                                        now_epoch=CLAIM_AT + 30)
        self.pub = self.services.publications.get(self.pub.id)
        return tuple(all_ids)


@pytest.fixture
def scenario(services) -> Scenario:
    return Scenario(services)


def _part_rows(conn, attempt_id):
    return conn.execute(
        "SELECT * FROM publication_parts WHERE attempt_id=?"
        " ORDER BY part_index", (attempt_id,)
    ).fetchall()


class TestPartIntents:
    def test_claim_snapshots_durable_intents(self, services, scenario):
        scenario.claim()
        rows = _part_rows(services.conn, scenario.attempt)
        assert [r["part_index"] for r in rows] == [1, 2, 3]
        assert all(r["state"] == "pending" for r in rows)
        assert all(r["part_count"] == 3 for r in rows)
        assert all(r["channel_message_ids"] is None for r in rows)

    def test_intent_snapshot_is_the_frozen_planned_part(self, services,
                                                        scenario):
        scenario.claim()
        parts = scenario.receipts.parts_of_attempt(scenario.attempt)
        assert [p.snapshot for p in parts] == list(scenario.plan.parts)

    def test_legacy_claim_invents_no_parts(self, services):
        post_id = services.drafts.create(text_content(), now_epoch=NOW)
        pub = services.publications.schedule(post_id, future(), now_epoch=NOW)
        attempt = services.dispatch.claim(pub.id, pub.version,
                                          now_epoch=CLAIM_AT)
        assert scenario_free_reads(services, attempt) == []

    def test_tampered_plan_aborts_the_claim_atomically(self, services,
                                                       scenario):
        services.conn.execute(
            "UPDATE post_plans SET plan_json=? WHERE post_id=?",
            ("{ not json", scenario.post_id))
        with pytest.raises(ContentValidationError):
            scenario.claim()
        pub = services.publications.get(scenario.pub.id)
        assert pub.status.value == "scheduled"
        assert pub.version == scenario.pub.version
        assert services.conn.execute(
            "SELECT COUNT(*) c FROM send_attempts").fetchone()["c"] == 0
        assert services.conn.execute(
            "SELECT COUNT(*) c FROM publication_parts").fetchone()["c"] == 0

    def test_single_part_plans_get_one_intent(self, services):
        logical = LogicalPost(body="короткий текст")
        plan = plan_for(logical)
        post_id = services.drafts.create(plan.parts[0].content, now_epoch=NOW)
        PlanStore(services.conn).save(post_id, logical, plan, now_epoch=NOW)
        pub = services.publications.schedule(post_id, future(), now_epoch=NOW)
        attempt = services.dispatch.claim(pub.id, pub.version,
                                          now_epoch=CLAIM_AT)
        parts = PartReceiptStore(services.conn).parts_of_attempt(attempt)
        assert [(p.part_index, p.part_count) for p in parts] == [(1, 1)]


def scenario_free_reads(services, attempt):
    return PartReceiptStore(services.conn).parts_of_attempt(attempt)


class TestReceipts:
    def test_confirmed_receipt_stores_the_whole_album_list(self, services,
                                                           scenario):
        scenario.claim()
        part = scenario.confirm(1, (9103, 9101, 9102))
        assert part.state == "confirmed"
        assert part.channel_message_ids == (9103, 9101, 9102)
        assert part.permalink == "https://t.me/c/777/9103"
        assert part.confirmed_at is not None
        import json as _json
        row = _part_rows(services.conn, scenario.attempt)[0]
        assert _json.loads(row["channel_message_ids"]) == [9103, 9101, 9102]

    def test_receipt_validators(self, scenario):
        scenario.claim()
        with pytest.raises(TransitionError):
            scenario.receipts.record_confirmed(scenario.attempt, 1, (),
                                               "https://t.me/c/777/1")
        with pytest.raises(TransitionError):
            scenario.receipts.record_confirmed(scenario.attempt, 1, (0,),
                                               "https://t.me/c/777/1")
        with pytest.raises(TransitionError):
            scenario.receipts.record_confirmed(scenario.attempt, 1, (-5,),
                                               "https://t.me/c/777/1")
        with pytest.raises(TransitionError):
            scenario.receipts.record_confirmed(scenario.attempt, 1,
                                               (1, True),
                                               "https://t.me/c/777/1")
        with pytest.raises(TransitionError):
            scenario.receipts.record_confirmed(scenario.attempt, 1, (1,), "")
        with pytest.raises(TransitionError):
            scenario.receipts.record_confirmed(
                scenario.attempt, 1, (1,), "https://t.me/x not a url")

    def test_unknown_part_or_attempt(self, scenario):
        scenario.claim()
        with pytest.raises(PartNotFound):
            scenario.receipts.record_confirmed(scenario.attempt, 0, (1,),
                                               "https://t.me/c/777/1")
        with pytest.raises(PartNotFound):
            scenario.receipts.record_confirmed(scenario.attempt, 4, (1,),
                                               "https://t.me/c/777/1")
        with pytest.raises(PartNotFound):
            scenario.receipts.record_confirmed(424242, 1, (1,),
                                               "https://t.me/c/777/1")

    def test_identical_ack_replay_is_idempotent(self, services, scenario):
        scenario.claim()
        first = scenario.confirm(1, (9101,), now_epoch=CLAIM_AT + 5)
        again = scenario.receipts.record_confirmed(
            scenario.attempt, 1, (9101,), "https://t.me/c/777/9101",
            now_epoch=CLAIM_AT + 999)
        assert again.state == "confirmed"
        assert again.confirmed_at == first.confirmed_at
        assert again.updated_at == first.updated_at
        assert len(_part_rows(services.conn, scenario.attempt)) == 3

    def test_different_replay_is_refused_and_changes_nothing(
            self, services, scenario):
        scenario.claim()
        scenario.confirm(1, (9101,))
        before = _part_rows(services.conn, scenario.attempt)[0]
        with pytest.raises(TransitionError):
            scenario.confirm(1, (9101, 9102))
        with pytest.raises(TransitionError):
            scenario.receipts.record_confirmed(scenario.attempt, 1, (9101,),
                                               "https://t.me/c/777/other",
                                               now_epoch=CLAIM_AT + 6)
        after = _part_rows(services.conn, scenario.attempt)[0]
        assert tuple(after) == tuple(before)

    def test_exact_replay_after_publish_still_idempotent(self, services,
                                                         scenario):
        scenario.claim()
        scenario.publish()  # attempt finished, publication published
        replay = scenario.receipts.record_confirmed(
            scenario.attempt, 1, (9001,), "https://t.me/c/777/9001",
            now_epoch=CLAIM_AT + 60)
        assert replay.state == "confirmed"

    def test_pending_part_of_resolved_attempt_cannot_be_received(
            self, services, scenario):
        scenario.claim()
        scenario.confirm(1, (9001,))
        scenario.services.dispatch.mark_uncertain(scenario.pub.id,
                                                  scenario.attempt,
                                                  "partial_result",
                                                  now_epoch=CLAIM_AT + 10)
        with pytest.raises(TransitionError):
            scenario.receipts.record_confirmed(scenario.attempt, 2, (9002,),
                                               "https://t.me/c/777/9002",
                                               now_epoch=CLAIM_AT + 20)


class TestAckPermalinkGate:
    """The storage gate refuses acks a real channel could never have sent.

    The config layer is HTTPS-only hardened (test_config.py); the same rule
    is enforced DIRECTLY at the durable receipt boundary
    (repo.receipts._validate_ack): any non-HTTPS scheme and any userinfo
    credential ('user@', 'user:pw@') is refused BEFORE the transaction even
    opens, with fixed error texts that never echo the offending value. A
    refused ack must leave the claim's intent rows exactly as the claim
    transaction wrote them.
    """

    @pytest.mark.parametrize("bad", [
        "ftp://files.example/doc",
        "javascript:alert(1)",
        "http://MARKERVAL.example/c/777/9101",
        "https://",
        "https:///c/777/9101",
        "https://owner:MARKERpass@MARKERVAL.example/c/777/9101",
        "https://owner@MARKERVAL.example/c/777/9101",
    ])
    def test_non_https_or_credentialed_permalink_is_refused(self, scenario,
                                                           bad):
        scenario.claim()
        with pytest.raises(TransitionError) as caught:
            scenario.receipts.record_confirmed(scenario.attempt, 1, (9101,),
                                               bad)
        assert "MARKER" not in str(caught.value)   # fixed text, no value echo

    def test_http_refusal_names_the_https_rule(self, scenario):
        scenario.claim()
        with pytest.raises(TransitionError) as caught:
            scenario.receipts.record_confirmed(
                scenario.attempt, 1, (9101,),
                "http://MARKERVAL.example/c/777/9101")
        text = str(caught.value)
        assert "https" in text       # the rule names the only accepted scheme
        assert "MARKERVAL" not in text

    def test_credentials_refusal_names_the_rule_without_echoing_the_secret(
            self, scenario):
        scenario.claim()
        with pytest.raises(TransitionError) as caught:
            scenario.receipts.record_confirmed(
                scenario.attempt, 1, (9101,),
                "https://owner:MARKERpass@MARKERVAL.example/c/777/9101")
        text = str(caught.value)
        assert "credentials" in text
        assert "MARKERpass" not in text
        assert "MARKERVAL" not in text

    def test_plain_https_permalink_is_accepted_verbatim(self, services,
                                                        scenario):
        scenario.claim()
        good = "https://t.me/c/777/9101"
        part = scenario.receipts.record_confirmed(scenario.attempt, 1, (9101,),
                                                  good)
        assert part.state == "confirmed"
        assert part.permalink == good
        assert _part_rows(services.conn, scenario.attempt)[0][
            "permalink"] == good

    def test_credentialed_replay_after_a_good_receipt_is_refused(self,
                                                                 services,
                                                                 scenario):
        # the gate runs before the idempotency comparison: a crafted replay
        # carrying credentials never overwrites and never echoes its secret
        scenario.claim()
        scenario.confirm(1, (9101,))
        before = _part_rows(services.conn, scenario.attempt)[0]
        with pytest.raises(TransitionError) as caught:
            scenario.receipts.record_confirmed(
                scenario.attempt, 1, (9101,),
                "https://owner:MARKERpass@t.me/c/777/9101")
        assert "MARKERpass" not in str(caught.value)
        after = _part_rows(services.conn, scenario.attempt)[0]
        assert tuple(after) == tuple(before)
        assert after["permalink"] == "https://t.me/c/777/9101"

    def test_refused_acks_leave_the_claim_intents_untouched(self, services,
                                                            scenario):
        scenario.claim()
        intents_before = [tuple(r) for r in
                          _part_rows(services.conn, scenario.attempt)]
        for part_index, bad in (
                (1, "http://MARKERVAL.example/c/777/9101"),
                (2, "https://owner:MARKERpass@MARKERVAL.example/c/777/9102"),
                (3, "https:///c/777/9103")):
            with pytest.raises(TransitionError):
                scenario.receipts.record_confirmed(scenario.attempt, part_index,
                                                   (9100 + part_index,), bad)
        after = _part_rows(services.conn, scenario.attempt)
        assert [tuple(r) for r in after] == intents_before
        assert [(r["part_index"], r["part_count"], r["state"]) for r in after] \
            == [(1, 3, "pending"), (2, 3, "pending"), (3, 3, "pending")]
        assert all(r["channel_message_ids"] is None and r["permalink"] is None
                   for r in after)
        # the gate never poisons the intent: the real ack still lands
        scenario.confirm(1, (9101,))
        assert scenario.receipts.parts_of_attempt(scenario.attempt)[0].state \
            == "confirmed"


class TestPartOutcomes:
    def test_uncertain_then_late_ack_is_refused(self, scenario):
        scenario.claim()
        part = scenario.receipts.record_uncertain(scenario.attempt, 2,
                                                  now_epoch=CLAIM_AT + 6)
        assert part.state == "uncertain"
        with pytest.raises(TransitionError):
            scenario.confirm(2, (9002,))
        replay = scenario.receipts.record_uncertain(scenario.attempt, 2,
                                                    now_epoch=CLAIM_AT + 7)
        assert replay.state == "uncertain"

    def test_failed_part_keeps_later_parts_pending_and_stable(
            self, services, scenario):
        scenario.claim()
        scenario.confirm(1, (9001,))
        failed = scenario.receipts.record_failed(scenario.attempt, 2,
                                                 now_epoch=CLAIM_AT + 6)
        assert failed.state == "failed"
        scenario.confirm(3, (9003,))
        rows = _part_rows(services.conn, scenario.attempt)
        assert [r["state"] for r in rows] == ["confirmed", "failed",
                                              "confirmed"]
        assert scenario.receipts.all_parts_confirmed(
            scenario.attempt) is False

    def test_outcome_of_confirmed_part_is_refused(self, scenario):
        scenario.claim()
        scenario.confirm(1, (9001,))
        with pytest.raises(TransitionError):
            scenario.receipts.record_uncertain(scenario.attempt, 1)
        with pytest.raises(TransitionError):
            scenario.receipts.record_failed(scenario.attempt, 1)


class TestCompleteGuard:
    def test_publish_needs_every_part_received(self, services, scenario):
        scenario.claim()
        scenario.confirm(1, (9001,))
        scenario.confirm(2, (9002,))
        with pytest.raises(TransitionError):
            services.dispatch.complete(scenario.pub.id, scenario.attempt,
                                       (9001, 9002),
                                       "https://t.me/c/777/9001",
                                       now_epoch=CLAIM_AT + 30)
        assert services.publications.get(scenario.pub.id).status.value \
            == "sending"
        scenario.confirm(3, (9003,))
        services.dispatch.complete(scenario.pub.id, scenario.attempt,
                                   (9001, 9002, 9003),
                                   "https://t.me/c/777/9001",
                                   now_epoch=CLAIM_AT + 31)
        assert services.publications.get(scenario.pub.id).status.value \
            == "published"

    def test_history_uses_the_union_and_one_canonical_permalink(
            self, services, scenario):
        scenario.claim()
        all_ids = scenario.publish()
        history = services.dispatch.history_links(scenario.post_id)
        assert len(history) == 1
        import json as _json
        assert _json.loads(history[0]["channel_message_ids"]) == \
            sorted(all_ids)
        assert history[0]["permalink"] == f"https://t.me/c/777/{all_ids[0]}"

    def test_completion_ids_must_match_the_recorded_union(self, services,
                                                          scenario):
        scenario.claim()
        scenario.confirm(1, (9011,))
        scenario.confirm(2, (9012,))
        scenario.confirm(3, (9013,))
        with pytest.raises(TransitionError):
            services.dispatch.complete(scenario.pub.id, scenario.attempt,
                                       (9011, 9012, 9013, 9014),
                                       "https://t.me/c/777/9011",
                                       now_epoch=CLAIM_AT + 30)
        with pytest.raises(TransitionError):
            services.dispatch.complete(scenario.pub.id, scenario.attempt,
                                       (9011, 9012),
                                       "https://t.me/c/777/9011",
                                       now_epoch=CLAIM_AT + 30)

    def test_completion_permalink_needs_receipt_provenance(self, services,
                                                           scenario):
        scenario.claim()
        scenario.confirm(1, (9011,))
        scenario.confirm(2, (9012,))
        scenario.confirm(3, (9013,))
        with pytest.raises(TransitionError):
            services.dispatch.complete(scenario.pub.id, scenario.attempt,
                                       (9011, 9012, 9013),
                                       "https://evil.example/1",
                                       now_epoch=CLAIM_AT + 30)

    def test_completion_wrong_attempt_still_guarded(self, services,
                                                    scenario):
        scenario.claim()
        scenario.confirm(1, (9001,))
        scenario.confirm(2, (9002,))
        scenario.confirm(3, (9003,))
        with pytest.raises(AttemptNotFound):
            services.dispatch.complete(scenario.pub.id,
                                       scenario.attempt + 1000,
                                       (9001, 9002, 9003),
                                       "https://t.me/c/777/9001",
                                       now_epoch=CLAIM_AT + 30)
        assert services.publications.get(scenario.pub.id).status.value \
            == "sending"



class TestNavigationEdit:
    """Navigation edits are separate durable attempts on RECEIVED parts.

    Gate (approved brief): only a part whose own receipt is 'confirmed' can
    be edited, and only once the publication completed as 'published'. For a
    normally finished attempt every part must carry a receipt first; for an
    owner-resolved publication (attempt abandoned by recovery, verified
    manually in the channel) editing the received parts stays possible —
    that is the deliberate escape hatch, never an automatic retry.
    """

    def _published(self, scenario):
        scenario.claim()
        scenario.publish()
        return scenario

    def test_blocked_while_publication_sending(self, scenario):
        scenario.claim()
        scenario.confirm(1, (9001,))
        with pytest.raises(TransitionError):
            scenario.receipts.begin_nav_edit(scenario.attempt, 1,
                                             now_epoch=CLAIM_AT + 10)

    def test_finished_attempt_needs_every_part_received(self, services,
                                                        scenario):
        # a PUBLISHED publication with an unreceipted part can only exist via
        # the owner-resolved path (dispatch.complete refuses otherwise):
        # editing the received part is allowed, editing the unreceipted one
        # is refused.
        scenario.claim()
        scenario.confirm(1, (9001,))
        services.dispatch.mark_uncertain(scenario.pub.id, scenario.attempt,
                                         "timeout", now_epoch=CLAIM_AT + 10)
        pub = services.publications.get(scenario.pub.id)
        services.dispatch.resolve_uncertain_as_published(
            pub.id, pub.version, (9001,), "https://t.me/c/777/9001",
            now_epoch=CLAIM_AT + 11)
        with pytest.raises(TransitionError):
            scenario.receipts.begin_nav_edit(scenario.attempt, 2,
                                             now_epoch=CLAIM_AT + 12)
        edit = scenario.receipts.begin_nav_edit(scenario.attempt, 1,
                                                now_epoch=CLAIM_AT + 13)
        assert edit.state == "pending"

    def test_full_lifecycle(self, scenario):
        self._published(scenario)
        receipts = scenario.receipts
        edit = receipts.begin_nav_edit(scenario.attempt, 1,
                                       now_epoch=CLAIM_AT + 1)
        assert edit.state == "pending" and edit.updated_at == iso(CLAIM_AT + 1)
        receipts.confirm_nav_edit(scenario.attempt, 1,
                                  now_epoch=CLAIM_AT + 2)
        assert receipts.get_nav_edit(scenario.attempt, 1).state == "confirmed"
        # a later navigation change starts a fresh attempt on the same part
        receipts.begin_nav_edit(scenario.attempt, 1, now_epoch=CLAIM_AT + 3)
        failed = receipts.fail_nav_edit(scenario.attempt, 1, "timeout",
                                        now_epoch=CLAIM_AT + 4)
        assert failed.state == "failed"
        assert failed.last_error == "timeout"

    def test_begin_is_idempotent_while_pending(self, scenario):
        self._published(scenario)
        receipts = scenario.receipts
        first = receipts.begin_nav_edit(scenario.attempt, 1,
                                        now_epoch=CLAIM_AT + 1)
        again = receipts.begin_nav_edit(scenario.attempt, 1,
                                        now_epoch=CLAIM_AT + 999)
        assert again.created_at == first.created_at
        assert again.updated_at == first.updated_at

    def test_confirm_or_fail_without_begin_rejected(self, scenario):
        self._published(scenario)
        with pytest.raises(PartNotFound):
            scenario.receipts.confirm_nav_edit(scenario.attempt, 1,
                                               now_epoch=CLAIM_AT + 1)
        with pytest.raises(PartNotFound):
            scenario.receipts.fail_nav_edit(scenario.attempt, 2, "timeout",
                                            now_epoch=CLAIM_AT + 1)

    def test_confirm_after_fail_rejected(self, scenario):
        self._published(scenario)
        receipts = scenario.receipts
        receipts.begin_nav_edit(scenario.attempt, 1, now_epoch=CLAIM_AT + 1)
        receipts.fail_nav_edit(scenario.attempt, 1, "timeout",
                               now_epoch=CLAIM_AT + 2)
        with pytest.raises(TransitionError):
            receipts.confirm_nav_edit(scenario.attempt, 1,
                                      now_epoch=CLAIM_AT + 3)

    def test_fail_after_confirm_rejected(self, scenario):
        self._published(scenario)
        receipts = scenario.receipts
        receipts.begin_nav_edit(scenario.attempt, 1, now_epoch=CLAIM_AT + 1)
        receipts.confirm_nav_edit(scenario.attempt, 1, now_epoch=CLAIM_AT + 2)
        with pytest.raises(TransitionError):
            receipts.fail_nav_edit(scenario.attempt, 1, "timeout",
                                   now_epoch=CLAIM_AT + 3)

    def test_unknown_part_or_attempt(self, scenario):
        self._published(scenario)
        with pytest.raises(PartNotFound):
            scenario.receipts.begin_nav_edit(scenario.attempt, 9,
                                             now_epoch=CLAIM_AT + 1)
        with pytest.raises(PartNotFound):
            scenario.receipts.begin_nav_edit(424242, 1,
                                             now_epoch=CLAIM_AT + 1)

    def test_receipt_rows_never_touched_by_navigation_edits(self, services,
                                                           scenario):
        self._published(scenario)
        receipts = scenario.receipts
        before = [dict(r) for r in services.conn.execute(
            "SELECT * FROM publication_parts ORDER BY id")]
        receipts.begin_nav_edit(scenario.attempt, 1, now_epoch=CLAIM_AT + 1)
        receipts.fail_nav_edit(scenario.attempt, 1, "timeout",
                               now_epoch=CLAIM_AT + 2)
        receipts.begin_nav_edit(scenario.attempt, 1, now_epoch=CLAIM_AT + 3)
        receipts.confirm_nav_edit(scenario.attempt, 1, now_epoch=CLAIM_AT + 4)
        after = [dict(r) for r in services.conn.execute(
            "SELECT * FROM publication_parts ORDER BY id")]
        assert before == after   # published bodies stay immutable


class TestReplanDistinction:
    def test_new_attempt_gets_fresh_intents_old_receipts_survive(
            self, services, scenario):
        scenario.claim()
        scenario.confirm(1, (9001,))
        scenario.receipts.record_uncertain(scenario.attempt, 2,
                                           now_epoch=CLAIM_AT + 5)
        services.dispatch.mark_uncertain(scenario.pub.id, scenario.attempt,
                                         "timeout", now_epoch=CLAIM_AT + 6)
        pub = services.publications.get(scenario.pub.id)
        pub = services.publications.replan(pub.id, pub.version, future(240),
                                           now_epoch=CLAIM_AT + 10)
        second = services.dispatch.claim(pub.id, pub.version,
                                         now_epoch=NOW + 14400)
        fresh = scenario.receipts.parts_of_attempt(second)
        assert [(p.part_index, p.state) for p in fresh] == [(1, "pending"),
                                                            (2, "pending"),
                                                            (3, "pending")]
        old = {p.part_index: p.state for p in
               scenario.receipts.parts_of_attempt(scenario.attempt)}
        assert old == {1: "confirmed", 2: "uncertain", 3: "pending"}


class TestSchemaConstraints:
    """The DB itself enforces the intent/receipt invariants — not only the
    store methods (defense in depth against any future writer)."""

    def _row(self, attempt_id, index, count, state, *, permalink=None,
             ids=None):
        return {
            "attempt_id": attempt_id, "part_index": index,
            "part_count": count, "state": state,
            "part_snapshot_json": json.dumps({"index": index}),
            "channel_message_ids": None if ids is None else json.dumps(ids),
            "permalink": permalink, "created_at": iso(NOW),
            "updated_at": iso(NOW), "confirmed_at": None,
        }

    def _insert(self, conn, row):
        conn.execute(
            "INSERT INTO publication_parts (attempt_id, part_index,"
            " part_count, state, part_snapshot_json,"
            " channel_message_ids, permalink, created_at, updated_at,"
            " confirmed_at) VALUES (:attempt_id, :part_index, :part_count,"
            " :state, :part_snapshot_json, :channel_message_ids,"
            " :permalink, :created_at, :updated_at, :confirmed_at)", row)

    def test_part_index_starts_at_one(self, services, scenario):
        scenario.claim()
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(services.conn,
                         self._row(scenario.attempt, 0, 3, "pending"))

    def test_part_index_within_part_count(self, services, scenario):
        scenario.claim()
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(services.conn,
                         self._row(scenario.attempt, 2, 1, "pending"))

    def test_confirmed_needs_permalink_and_ids(self, services, scenario):
        scenario.claim()
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(services.conn,
                         self._row(scenario.attempt, 1, 3, "confirmed",
                                   ids=[9001]))
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(services.conn,
                         self._row(scenario.attempt, 1, 3, "confirmed",
                                   permalink="https://t.me/c/777/9001"))

    def test_ids_column_shape_is_a_json_list(self, services, scenario):
        scenario.claim()
        row = self._row(scenario.attempt, 1, 3, "pending")
        row["channel_message_ids"] = "9001"
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(services.conn, row)

    def test_snapshot_must_be_a_json_object(self, services, scenario):
        scenario.claim()
        row = self._row(scenario.attempt, 1, 3, "pending")
        row["part_snapshot_json"] = "[1, 2]"
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(services.conn, row)

    def test_unknown_state_rejected(self, services, scenario):
        scenario.claim()
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(services.conn,
                         self._row(scenario.attempt, 1, 3, "maybe"))

    def test_attempt_fk_enforced(self, services):
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(services.conn, self._row(4242, 1, 3, "pending"))

    def test_one_intent_row_per_attempt_and_index(self, services, scenario):
        scenario.claim()
        self._insert(services.conn,
                     self._row(scenario.attempt, 9, 9, "pending"))
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(services.conn,
                         self._row(scenario.attempt, 9, 9, "failed"))

    def test_one_navigation_row_per_part(self, services, scenario):
        scenario.claim()
        scenario.publish()
        scenario.receipts.begin_nav_edit(scenario.attempt, 1,
                                         now_epoch=CLAIM_AT + 1)
        with pytest.raises(sqlite3.IntegrityError):
            services.conn.execute(
                "INSERT INTO navigation_edits (part_id, state, created_at,"
                " updated_at) SELECT id, 'pending', ?, ? FROM"
                " publication_parts WHERE attempt_id=? AND part_index=1",
                (iso(NOW + 1), iso(NOW + 1), scenario.attempt))

    def test_delete_post_cascades_plans_parts_and_navs(self, services,
                                                       scenario):
        scenario.claim()
        scenario.publish()
        scenario.receipts.begin_nav_edit(scenario.attempt, 1,
                                         now_epoch=CLAIM_AT + 1)
        services.publications.delete_by_post(scenario.post_id,
                                             now_epoch=CLAIM_AT + 2)
        for table in ("post_plans", "publication_parts", "navigation_edits"):
            assert services.conn.execute(
                f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0
        assert services.conn.execute(
            "PRAGMA foreign_key_check").fetchall() == []
