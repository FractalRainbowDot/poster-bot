"""Trusted native runtime context and read-only readiness, no remote services."""
import asyncio

import pytest

from conftest import NOW, future
from planned_helpers import SIG
from test_original_forward_dispatch import native_publication
from test_original_forward_sender import FakeAPI
from poster_bot.domain.content import Content, ForwardSource
from poster_bot.repo.notifications import OwnerNoticeStore
from poster_bot.repo.plans import PlanStore
from poster_bot.repo.receipts import PartReceiptStore
from poster_bot.repo.runtime_state import RuntimeState
from poster_bot.runtime import Runtime, plan_migration_readiness
from poster_bot.transport.aiogram_channel import AiogramChannelTransport


@pytest.mark.parametrize("binding,ready", [
    ({"owner_user_id": 123, "channel_id": -100999}, True),
    ({}, False), ({"owner_user_id": 123}, False),
    ({"owner_user_id": 124, "channel_id": -100999}, False),
    ({"owner_user_id": 123, "channel_id": -100998}, False)])
def test_readiness_requires_independent_binding_and_never_writes(services, binding, ready):
    pub, _ = native_publication(services)
    before = services.conn.total_changes
    report = plan_migration_readiness(services.conn, SIG, **binding)
    assert report.ready_for_cutover is ready
    assert report.items[0].publication_id == pub.id
    assert services.conn.total_changes == before


@pytest.mark.parametrize("column,value", [
    ("logical_json", '{"v":3,"mode":"unsupported"}'),
    ("plan_version", 999), ("plan_json", '{"v":999}')])
def test_corrupt_native_pair_is_readiness_blocker(services, column, value):
    pub, _ = native_publication(services)
    # Parameterized values; columns are fixed test literals above.
    services.conn.execute(f"UPDATE post_plans SET {column}=? WHERE post_id=?",
                          (value, pub.post_id))
    report = plan_migration_readiness(services.conn, SIG,
        owner_user_id=123, channel_id=-100999)
    assert not report.ready_for_cutover
    assert report.items[0].codes == ("plan_corrupt",)


def test_native_projection_mismatch_is_blocker(services):
    pub, _ = native_publication(services)
    wrong = Content(kind="forward", mode="forward", forward=ForwardSource(123, (90,)))
    services.conn.execute("UPDATE posts SET content_json=? WHERE id=?",
                          (wrong.to_json(), pub.post_id))
    assert not plan_migration_readiness(services.conn, SIG,
        owner_user_id=123, channel_id=-100999).ready_for_cutover


def runtime(services, api, **binding):
    return Runtime(dispatch=services.dispatch,
        notices=OwnerNoticeStore(services.conn, services.journal),
        runtime_state=RuntimeState(services.conn),
        transport=AiogramChannelTransport(api, owner_id=123, channel_id=-100999),
        plans=PlanStore(services.conn), receipts=PartReceiptStore(services.conn),
        signature=SIG, **binding)


@pytest.mark.parametrize("binding,expected", [
    ({"owner_user_id": 123, "channel_id": -100999}, ["published"]),
    ({}, [])])
def test_runtime_context_reaches_sender(services, binding, expected):
    pub, _ = native_publication(services)
    api = FakeAPI(services)
    rt = runtime(services, api, **binding)
    report = asyncio.run(rt.tick(now_epoch=pub.scheduled_epoch))
    assert [result.outcome for result in report.sent] == expected
    assert len(api.calls) == len(expected)


def test_legacy_raw_forward_remains_blocked_even_with_context(services):
    content = Content(kind="forward", mode="forward", forward=ForwardSource(123, (11,)))
    post_id = services.drafts.create(content, now_epoch=NOW)
    pub = services.publications.schedule(post_id, future(), now_epoch=NOW)
    report = plan_migration_readiness(services.conn, SIG,
        owner_user_id=123, channel_id=-100999)
    assert not report.ready_for_cutover and report.items[0].codes == ("forward",)
    api = FakeAPI(services)
    result = asyncio.run(runtime(services, api, owner_user_id=123,
        channel_id=-100999).tick(now_epoch=pub.scheduled_epoch))
    assert not result.sent and not api.calls
    assert not services.dispatch.unfinished_attempts()
