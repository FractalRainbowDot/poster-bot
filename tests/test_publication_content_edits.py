"""Real SQLite regression checks for non-sending publication content edits."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from conftest import NOW, future, text_content
from fake_transport import FakeTransport
from planned_helpers import SIG_OTHER, gated_sender, planned_publication
from poster_bot.domain.content import MediaRef
from poster_bot.domain.errors import ContentValidationError, PastTimeError, TransitionError
from poster_bot.domain.planner import LogicalPost, PublicationPlan, plan_post
from poster_bot.domain.states import Status
from poster_bot.repo.plans import PlanStore
from test_domain_planner_signature import LEGACY_V2_JSON


EDITABLE = (Status.SCHEDULED, Status.SUSPENDED, Status.MISSED)


def prepared(services, status):
    pub, post_id, plan = planned_publication(
        services, LogicalPost(body="original body"))
    if status is Status.SUSPENDED:
        pub = services.publications.pause(pub.id, pub.version, now_epoch=NOW + 1)
    elif status is Status.MISSED:
        services.dispatch.recovery_sweep(now_epoch=pub.scheduled_epoch + 3600)
        pub = services.publications.get(pub.id)
    assert pub.status is status
    return pub, post_id, plan


def replacement():
    logical = LogicalPost(body="new paragraph\n\n" + "X" * 3000 + "\n\n" + "Y" * 3000)
    result = plan_post(logical, SIG_OTHER)
    assert result.plan is not None
    return logical, result.plan


def snapshot(services):
    # Includes timestamps, ids, bindings, attempts, journal and frozen bytes.
    return {
        table: [tuple(row) for row in services.conn.execute(
            f"SELECT * FROM {table} ORDER BY rowid")]
        for table in ("posts", "post_plans", "publications", "send_attempts", "journal_events")
    }


@pytest.mark.parametrize("status", EDITABLE)
def test_edit_preserves_identity_slot_and_never_becomes_sendable(services, status):
    pub, post_id, _ = prepared(services, status)
    before = dict(services.conn.execute(
        "SELECT * FROM posts WHERE id=?", (post_id,)).fetchone())
    pub_before = dict(services.conn.execute(
        "SELECT * FROM publications WHERE id=?", (pub.id,)).fetchone())
    plan_created = PlanStore(services.conn).get(post_id).created_at
    logical, plan = replacement()
    transport = FakeTransport()
    sender = gated_sender(services, transport)
    for step in range(2):
        now = pub.scheduled_epoch + 4000 if status is Status.MISSED else NOW + 10 + step
        edited = services.publications.edit_content(
            pub.id, pub.version + step, plan.parts[0].content,
            logical=logical, plan=plan, now_epoch=now)
        assert edited.status is (Status.MISSED if status is Status.MISSED else Status.SUSPENDED)
        assert edited.version == pub.version + step + 1
        assert (edited.id, edited.post_id, edited.minute_epoch, edited.scheduled_epoch) == (
            pub.id, post_id, pub.minute_epoch, pub.scheduled_epoch)
        current = dict(services.conn.execute(
            "SELECT * FROM publications WHERE id=?", (pub.id,)).fetchone())
        for key in ("created_at", "scheduled_at", "confirmed_at", "mode"):
            assert current[key] == pub_before[key]
        post = dict(services.conn.execute(
            "SELECT * FROM posts WHERE id=?", (post_id,)).fetchone())
        assert post["created_at"] == before["created_at"]
        assert post["id"] == before["id"]
        assert services.drafts.get_content(post_id) == plan.parts[0].content
        stored = PlanStore(services.conn).get(post_id)
        assert stored.post_id == post_id
        assert stored.created_at == plan_created
        assert stored.logical == logical
        assert stored.plan.to_json() == plan.to_json()
        assert pub.minute_epoch in services.publications.reserved_minutes()
        assert services.dispatch.due_publications(now_epoch=pub.scheduled_epoch) == []
        assert asyncio.run(sender.send_due(now_epoch=max(now, pub.scheduled_epoch))) == []
    assert transport.calls == transport.edit_calls == transport.notices == []
    assert services.conn.execute("SELECT COUNT(*) FROM send_attempts").fetchone()[0] == 0


@pytest.mark.parametrize("status", EDITABLE)
def test_stale_version_rolls_back_all_snapshots(services, status):
    pub, _, _ = prepared(services, status)
    before = snapshot(services)
    logical, plan = replacement()
    with pytest.raises(TransitionError):
        services.publications.edit_content(
            pub.id, pub.version - 1, plan.parts[0].content,
            logical=logical, plan=plan, now_epoch=NOW + 10)
    assert snapshot(services) == before


@pytest.mark.parametrize("status", (Status.SENDING, Status.UNCERTAIN, Status.PUBLISHED))
def test_forbidden_status_has_no_mutation(services, status):
    pub, _, _ = prepared(services, Status.SCHEDULED)
    # Status guards are independent of delivery receipts; no external send.
    services.conn.execute("UPDATE publications SET status=? WHERE id=?", (status.value, pub.id))
    before = snapshot(services)
    logical, plan = replacement()
    with pytest.raises(TransitionError):
        services.publications.edit_content(
            pub.id, pub.version, plan.parts[0].content,
            logical=logical, plan=plan, now_epoch=NOW + 10)
    assert snapshot(services) == before


@pytest.mark.parametrize("status", EDITABLE)
@pytest.mark.parametrize("failure", (
    "half_pair", "mismatch", "malformed_logical", "malformed_plan",
    "first_part_mismatch", "persist", "journal",
))
def test_validation_and_persistence_failure_are_atomic(services, status, failure):
    pub, _, _ = prepared(services, status)
    logical, plan = replacement()
    content = plan.parts[0].content
    kwargs = {"logical": logical, "plan": plan}
    error = ContentValidationError
    if failure == "half_pair":
        kwargs.pop("plan")
        error = TransitionError
    elif failure == "mismatch":
        kwargs["logical"] = LogicalPost(body="not the plan body")
    elif failure == "malformed_logical":
        kwargs["logical"] = "not a logical post"
    elif failure == "malformed_plan":
        kwargs["plan"] = "not a plan"
    elif failure == "first_part_mismatch":
        content = text_content("not the first physical part")
    elif failure == "persist":
        services.conn.execute(
            "CREATE TEMP TRIGGER reject_plan BEFORE UPDATE ON post_plans "
            "BEGIN SELECT RAISE(ABORT, 'test persistence failure'); END")
        error = sqlite3.IntegrityError
    else:
        services.conn.execute(
            "CREATE TEMP TRIGGER reject_event BEFORE INSERT ON journal_events "
            "BEGIN SELECT RAISE(ABORT, 'test journal failure'); END")
        error = sqlite3.IntegrityError
    before = snapshot(services)
    with pytest.raises(error):
        services.publications.edit_content(
            pub.id, pub.version, content, **kwargs, now_epoch=NOW + 10)
    assert snapshot(services) == before


@pytest.mark.parametrize("status", EDITABLE)
def test_planned_post_cannot_desynchronize_content_without_pair(services, status):
    pub, _, _ = prepared(services, status)
    before = snapshot(services)
    with pytest.raises(TransitionError):
        services.publications.edit_content(
            pub.id, pub.version, text_content("plan would be stale"), now_epoch=NOW + 10)
    assert snapshot(services) == before


@pytest.mark.parametrize("status", EDITABLE)
def test_old_status_cas_rejects_status_change_even_without_version_change(services, status):
    pub, _, _ = prepared(services, status)
    logical, plan = replacement()
    # Adversarial in-transaction mutation proves UPDATE guards the exact old
    # status, not merely membership in the wider editable status set.
    services.conn.execute(
        "CREATE TEMP TRIGGER change_status BEFORE UPDATE ON posts BEGIN "
        "UPDATE publications SET status='uncertain'; END")
    before = snapshot(services)
    with pytest.raises(TransitionError):
        services.publications.edit_content(
            pub.id, pub.version, plan.parts[0].content,
            logical=logical, plan=plan, now_epoch=NOW + 10)
    assert snapshot(services) == before


@pytest.mark.parametrize("status", (Status.SUSPENDED, Status.MISSED))
def test_plain_content_past_slot_edit_does_not_reconfirm_or_replan(services, status):
    post_id = services.drafts.create(text_content("original"), now_epoch=NOW)
    pub = services.publications.schedule(post_id, future(), now_epoch=NOW)
    pub = services.publications.pause(pub.id, pub.version, now_epoch=NOW + 1)
    past = pub.scheduled_epoch + 3600
    if status is Status.MISSED:
        services.dispatch.recovery_sweep(now_epoch=past)
        pub = services.publications.get(pub.id)
    edited = services.publications.edit_content(
        pub.id, pub.version, text_content("past slot edit"), now_epoch=past)
    assert edited.status is status
    assert edited.scheduled_epoch == pub.scheduled_epoch
    assert edited.minute_epoch == pub.minute_epoch
    assert services.drafts.get_content(post_id).text == "past slot edit"
    assert not PlanStore(services.conn).has_plan(post_id)
    # Content editing relaxes no explicit scheduling/future-time constraints.
    with pytest.raises(PastTimeError):
        if status is Status.SUSPENDED:
            services.publications.resume(pub.id, edited.version, now_epoch=past)
        else:
            services.publications.replan(pub.id, edited.version, future(), now_epoch=past)


@pytest.mark.parametrize("status", (Status.SUSPENDED, Status.MISSED))
def test_legacy_v2_is_frozen_until_explicit_content_replacement(services, status):
    legacy = PublicationPlan.from_json(LEGACY_V2_JSON)
    logical = LogicalPost(media=(MediaRef(kind="photo", file_id="legacy", caption="x" * 1020),))
    post_id = services.drafts.create_with_plan(logical, legacy, now_epoch=NOW)
    pub = services.publications.schedule(post_id, future(), now_epoch=NOW)
    if status is Status.SUSPENDED:
        pub = services.publications.pause(pub.id, pub.version, now_epoch=NOW + 1)
    else:
        services.dispatch.recovery_sweep(now_epoch=pub.scheduled_epoch + 3600)
        pub = services.publications.get(pub.id)
    assert PlanStore(services.conn).get(post_id).plan.to_json() == LEGACY_V2_JSON
    logical, plan = replacement()  # ordinary current planner; explicit new signature
    edited = services.publications.edit_content(
        pub.id, pub.version, plan.parts[0].content, logical=logical, plan=plan,
        now_epoch=pub.scheduled_epoch + 4000)
    assert edited.status is status
    assert PlanStore(services.conn).get(post_id).plan.to_json() == plan.to_json()


def test_authoritative_status_and_version_are_read_inside_writer_transaction(services, monkeypatch):
    pub, _, _ = prepared(services, Status.SUSPENDED)
    logical, plan = replacement()
    get = services.publications.get
    reads_in_transaction = []

    def observed_get(pub_id):
        reads_in_transaction.append(services.conn.in_transaction)
        return get(pub_id)

    monkeypatch.setattr(services.publications, "get", observed_get)
    services.publications.edit_content(
        pub.id, pub.version, plan.parts[0].content, logical=logical, plan=plan,
        now_epoch=NOW + 10)
    assert reads_in_transaction == [True, False]
