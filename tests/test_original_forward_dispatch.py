"""Native claim is one durable operation, gated by trusted live binding."""
import json

import pytest

from conftest import NOW, future
from poster_bot.domain.errors import ContentValidationError
from poster_bot.domain.original_forward import (
    OriginalForwardInput, OriginalForwardSource, build_original_forward_plan,
    parse_send_snapshot,
)


def native_publication(services, ids=(11, 12, 13)):
    logical = OriginalForwardInput(OriginalForwardSource(
        123, ids, "accepted-album" if len(ids) > 1 else None))
    plan = build_original_forward_plan(logical, owner_user_id=123,
                                       channel_id=-100999)
    post_id = services.drafts.create_with_plan(logical, plan, now_epoch=NOW)
    pub = services.publications.schedule(post_id, future(), now_epoch=NOW)
    return pub, plan


@pytest.mark.parametrize("ids", [(11,), (11, 12, 13)])
def test_native_claim_snapshots_one_complete_operation(services, ids):
    pub, plan = native_publication(services, ids)
    attempt = services.dispatch.claim(
        pub.id, pub.version, now_epoch=pub.scheduled_epoch,
        owner_user_id=123, channel_id=-100999)
    rows = services.conn.execute(
        "SELECT * FROM publication_parts WHERE attempt_id=?", (attempt,)
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert (row["part_index"], row["part_count"], row["state"]) == (1, 1, "pending")
    assert row["part_snapshot_json"] == plan.to_json()
    assert parse_send_snapshot(row["part_snapshot_json"]) == plan
    assert services.publications.get(pub.id).status.value == "sending"


@pytest.mark.parametrize("context", [
    {}, {"owner_user_id": 123}, {"channel_id": -100999},
    {"owner_user_id": 124, "channel_id": -100999},
    {"owner_user_id": 123, "channel_id": -100998},
    {"owner_user_id": True, "channel_id": -100999},
])
def test_missing_or_mismatched_binding_leaves_no_claim(services, context):
    pub, _ = native_publication(services)
    with pytest.raises(ContentValidationError):
        services.dispatch.claim(pub.id, pub.version,
                                now_epoch=pub.scheduled_epoch, **context)
    assert services.publications.get(pub.id).status.value == "scheduled"
    assert services.publications.get(pub.id).version == pub.version
    assert not services.dispatch.unfinished_attempts()
    assert services.conn.execute("SELECT COUNT(*) FROM publication_parts").fetchone()[0] == 0


def test_native_claim_rejects_corrupt_plan_atomically(services):
    pub, plan = native_publication(services)
    data = json.loads(plan.to_json())
    data["source"]["message_ids"] = [11, True]
    services.conn.execute("UPDATE post_plans SET plan_json=? WHERE post_id=?",
                          (json.dumps(data), pub.post_id))
    with pytest.raises(ContentValidationError):
        services.dispatch.claim(pub.id, pub.version,
                                now_epoch=pub.scheduled_epoch,
                                owner_user_id=123, channel_id=-100999)
    assert services.publications.get(pub.id).status.value == "scheduled"
    assert not services.dispatch.unfinished_attempts()


def test_crash_after_native_claim_recovers_uncertain_never_due(services):
    pub, _ = native_publication(services)
    services.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch,
                            owner_user_id=123, channel_id=-100999)
    recovered = services.dispatch.recovery_sweep(now_epoch=pub.scheduled_epoch)
    assert recovered.uncertain_publication_ids == (pub.id,)
    assert services.dispatch.due_publications(now_epoch=pub.scheduled_epoch) == []
    assert not services.dispatch.unfinished_attempts()
