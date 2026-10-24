"""Isolated SQLite checks for explicit native-forward pairs and atomic edits."""
import sqlite3

import pytest

from conftest import NOW, future
from poster_bot.domain.errors import ContentValidationError, TransitionError
from poster_bot.domain.original_forward import (
    OriginalForwardInput, OriginalForwardSource, build_original_forward_plan,
    parse_publication_input, parse_publication_plan, plan_primary_content,
)
from poster_bot.domain.content import MediaRef
from poster_bot.domain.planner import LogicalPost, plan_post
from poster_bot.domain.states import Status
from poster_bot.repo.plans import PlanStore, validate_plan_pair
from test_domain_planner_signature import LEGACY_V2_JSON
from test_repo_plans import SIG


def pair(ids=(10, 11), group="album"):
    logical = OriginalForwardInput(OriginalForwardSource(42, ids, group))
    return logical, build_original_forward_plan(logical, owner_user_id=42, channel_id=-10077)


def rows(conn):
    return {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id" if table == "posts" else f"SELECT * FROM {table}")]
            for table in ("posts", "post_plans", "publications", "journal_events")}


def test_native_create_read_and_draft_resync(services):
    logical, plan = pair()
    post = services.drafts.create_with_plan(logical, plan, now_epoch=NOW)
    store = PlanStore(services.conn)
    assert store.get(post).plan == plan
    assert store.get(post).logical == logical
    assert services.drafts.get_content(post) == plan_primary_content(plan)
    new_input, new_plan = pair((20,), None)
    store.save_with_content(post, new_input, new_plan, plan_primary_content(new_plan), now_epoch=NOW + 1)
    assert store.get(post).plan == new_plan
    assert services.drafts.get_content(post).forward.message_ids == (20,)


@pytest.mark.parametrize("status", ["scheduled", "suspended", "missed"])
def test_native_edit_cas_preserves_slot_identity_and_non_sendable_status(services, status):
    logical, plan = pair()
    post = services.drafts.create_with_plan(logical, plan, now_epoch=NOW)
    pub = services.publications.schedule(post, future(), now_epoch=NOW)
    services.conn.execute("UPDATE publications SET status=? WHERE id=?", (status, pub.id))
    before = services.publications.get(pub.id)
    new_input, new_plan = pair((20,), None)
    edited = services.publications.edit_content(pub.id, before.version, plan_primary_content(new_plan),
        logical=new_input, plan=new_plan, now_epoch=NOW + 1)
    assert (edited.id, edited.post_id, edited.minute_epoch, edited.scheduled_epoch) == (
        before.id, post, before.minute_epoch, before.scheduled_epoch)
    assert edited.status is (Status.SUSPENDED if status == "scheduled" else Status(status))
    assert edited.version == before.version + 1
    assert PlanStore(services.conn).get(post).plan == new_plan
    stable = rows(services.conn)
    with pytest.raises(TransitionError):
        services.publications.edit_content(pub.id, before.version, plan_primary_content(plan), logical=logical, plan=plan)
    assert rows(services.conn) == stable


def test_invalid_pair_and_projection_write_nothing(services):
    logical, plan = pair()
    other_input, other_plan = pair((20,), None)
    before = rows(services.conn)
    with pytest.raises(ContentValidationError):
        services.drafts.create_with_plan(other_input, plan)
    assert rows(services.conn) == before
    post = services.drafts.create_with_plan(logical, plan, now_epoch=NOW)
    pub = services.publications.schedule(post, future(), now_epoch=NOW)
    before = rows(services.conn)
    for candidate_input, candidate_plan, content in [
        (other_input, plan, plan_primary_content(plan)),
        (other_input, other_plan, plan_primary_content(plan)),
    ]:
        with pytest.raises(ContentValidationError):
            services.publications.edit_content(pub.id, pub.version, content,
                logical=candidate_input, plan=candidate_plan)
        assert rows(services.conn) == before


def test_native_transaction_rolls_back_post_pair_and_journal(services):
    logical, plan = pair()
    services.conn.execute("CREATE TEMP TRIGGER reject_native_create BEFORE INSERT ON post_plans BEGIN SELECT RAISE(ABORT, 'test'); END")
    before = rows(services.conn)
    with pytest.raises(sqlite3.IntegrityError):
        services.drafts.create_with_plan(logical, plan, now_epoch=NOW)
    assert rows(services.conn) == before
    services.conn.execute("DROP TRIGGER reject_native_create")
    post = services.drafts.create_with_plan(logical, plan, now_epoch=NOW)
    pub = services.publications.schedule(post, future(), now_epoch=NOW)
    services.conn.execute("CREATE TEMP TRIGGER reject_native_update BEFORE UPDATE ON post_plans BEGIN SELECT RAISE(ABORT, 'test'); END")
    before = rows(services.conn)
    new_input, new_plan = pair((20,), None)
    with pytest.raises(sqlite3.IntegrityError):
        services.publications.edit_content(pub.id, pub.version, plan_primary_content(new_plan), logical=new_input, plan=new_plan)
    assert rows(services.conn) == before


def test_journal_failure_rolls_back_native_content_plan_and_cas(services):
    logical, plan = pair()
    post = services.drafts.create_with_plan(logical, plan)
    pub = services.publications.schedule(post, future(), now_epoch=NOW)
    services.conn.execute("CREATE TEMP TRIGGER reject_native_journal BEFORE INSERT ON journal_events BEGIN SELECT RAISE(ABORT, 'test'); END")
    before = rows(services.conn)
    new_input, new_plan = pair((20,), None)
    with pytest.raises(sqlite3.IntegrityError):
        services.publications.edit_content(pub.id, pub.version, plan_primary_content(new_plan), logical=new_input, plan=new_plan)
    assert rows(services.conn) == before


@pytest.mark.parametrize("status", ["sending", "uncertain", "published"])
def test_native_frozen_status_refuses_plan_and_content_edits(services, status):
    logical, plan = pair()
    post = services.drafts.create_with_plan(logical, plan)
    pub = services.publications.schedule(post, future(), now_epoch=NOW)
    services.conn.execute("UPDATE publications SET status=? WHERE id=?", (status, pub.id))
    before = rows(services.conn)
    with pytest.raises(TransitionError):
        PlanStore(services.conn).save(post, logical, plan)
    with pytest.raises(TransitionError):
        services.publications.edit_content(pub.id, pub.version, plan_primary_content(plan), logical=logical, plan=plan)
    assert rows(services.conn) == before


def test_native_read_rejects_pair_corruption_and_version_mismatch(services):
    logical, plan = pair()
    post = services.drafts.create_with_plan(logical, plan)
    other, _ = pair((20,), None)
    services.conn.execute("UPDATE post_plans SET logical_json=? WHERE post_id=?", (other.to_json(), post))
    with pytest.raises(ContentValidationError):
        PlanStore(services.conn).get(post)
    services.conn.execute("UPDATE post_plans SET logical_json=?, plan_version=3 WHERE post_id=?", (logical.to_json(), post))
    with pytest.raises(ContentValidationError):
        PlanStore(services.conn).get(post)


def test_native_save_and_read_require_exact_legacy_projection(services):
    logical, plan = pair()
    post = services.drafts.create(plan_primary_content(pair((99,), None)[1]))
    before = rows(services.conn)
    with pytest.raises(ContentValidationError):
        PlanStore(services.conn).save(post, logical, plan)
    assert rows(services.conn) == before
    PlanStore(services.conn).save_with_content(post, logical, plan, plan_primary_content(plan))
    services.conn.execute("UPDATE posts SET content_json=? WHERE id=?", (plan_primary_content(pair((99,), None)[1]).to_json(), post))
    with pytest.raises(ContentValidationError):
        PlanStore(services.conn).get(post)


def test_edit_signed_to_native_and_back_updates_publication_mode(services):
    copy_input = LogicalPost(body="copy")
    copy_plan = plan_post(copy_input, SIG).plan
    post = services.drafts.create_with_plan(copy_input, copy_plan)
    pub = services.publications.schedule(post, future(), now_epoch=NOW)
    logical, plan = pair()
    pub = services.publications.edit_content(pub.id, pub.version, plan_primary_content(plan), logical=logical, plan=plan)
    assert pub.mode == "forward"
    pub = services.publications.edit_content(pub.id, pub.version, plan_primary_content(copy_plan), logical=copy_input, plan=copy_plan)
    assert pub.mode == "copy"


def test_source_reference_query_includes_retained_intent_after_copy_conversion(services):
    logical, plan = pair()
    post = services.drafts.create_with_plan(logical, plan)
    store = PlanStore(services.conn)
    assert store.protected_original_forward_coordinates() == frozenset({(42, 10), (42, 11)})
    pub = services.publications.schedule(post, future(), now_epoch=NOW)
    attempt = services.conn.execute(
        "INSERT INTO send_attempts(publication_id,state,created_at) VALUES (?,'abandoned','test')", (pub.id,)).lastrowid
    services.conn.execute(
        "INSERT INTO publication_parts(attempt_id,part_index,part_count,state,part_snapshot_json,created_at,updated_at)"
        " VALUES (?,1,1,'uncertain',?,'test','test')", (attempt, plan.to_json()))
    copy_input = LogicalPost(body="copy")
    copy_plan = plan_post(copy_input, SIG).plan
    services.publications.edit_content(pub.id, pub.version, plan_primary_content(copy_plan), logical=copy_input, plan=copy_plan)
    assert store.protected_original_forward_coordinates() == frozenset({(42, 10), (42, 11)})
    services.conn.execute("UPDATE publication_parts SET part_snapshot_json='{broken'")
    with pytest.raises(ContentValidationError):
        store.protected_original_forward_coordinates()


def test_source_reference_query_fails_closed_on_unreadable_plan(services):
    logical, plan = pair()
    services.drafts.create_with_plan(logical, plan)
    services.conn.execute("UPDATE post_plans SET plan_json='{broken'")
    with pytest.raises(ContentValidationError):
        PlanStore(services.conn).protected_original_forward_coordinates()


def test_signed_versions_keep_canonical_bytes_and_reject_mixed_pairs(services):
    old_plan = parse_publication_plan(LEGACY_V2_JSON)
    assert old_plan.to_json() == LEGACY_V2_JSON
    for signed in (old_plan, plan_post(LogicalPost(body="copy"), SIG).plan):
        logical = (LogicalPost(media=(MediaRef(kind="photo", file_id="legacy", caption="x" * 1020),))
                   if signed.version == 2 else LogicalPost(body=signed.reconstructed_body()))
        raw_input, raw_plan = logical.to_json(), signed.to_json()
        assert parse_publication_input(raw_input).to_json() == raw_input
        assert parse_publication_plan(raw_plan).to_json() == raw_plan
        post = services.drafts.create_with_plan(logical, signed)
        assert PlanStore(services.conn).get(post).plan.to_json() == raw_plan
        native_input, native_plan = pair()
        with pytest.raises(ContentValidationError):
            validate_plan_pair(native_input, signed)
        with pytest.raises(ContentValidationError):
            validate_plan_pair(logical, native_plan)
