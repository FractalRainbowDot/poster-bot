"""Native sends use one durable intent and exact acknowledgements, offline."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import ForwardMessages

from test_original_forward_dispatch import native_publication
from poster_bot.domain.errors import ContentValidationError, TransitionError
from poster_bot.repo.plans import PlanStore
from poster_bot.repo.receipts import PartReceiptStore
from poster_bot.repo.sender import SenderService
from poster_bot.transport.aiogram_channel import AiogramChannelTransport
from poster_bot.transport.ports import Sent


class FakeAPI:
    def __init__(self, services, result=(303, 301, 302), error=None):
        self.services = services
        self.result = result
        self.error = error
        self.calls = []
        self.notices = []

    async def forward_messages(self, **kwargs):
        rows = self.services.conn.execute(
            "SELECT * FROM publication_parts").fetchall()
        assert len(rows) == 1 and rows[0]["state"] == "pending"
        assert (rows[0]["part_index"], rows[0]["part_count"]) == (1, 1)
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        if self.result is None:
            return None
        return [SimpleNamespace(message_id=mid) for mid in self.result]

    async def send_message(self, **kwargs):
        assert kwargs["chat_id"] == 123  # notices only, never signed text
        self.notices.append(kwargs)

    async def edit_published_part(self, request):
        pytest.fail("native forward must never edit navigation")


def sender(services, api, **binding):
    transport = AiogramChannelTransport(api, owner_id=123, channel_id=-100999)
    return SenderService(
        services.dispatch, transport, plans=PlanStore(services.conn),
        receipts=PartReceiptStore(services.conn), editor=api, **binding)


def send(service, pub):
    return asyncio.run(service.send_due(now_epoch=pub.scheduled_epoch))


def test_native_receipt_parser_reads_whole_intent(services):
    pub, plan = native_publication(services)
    attempt = services.dispatch.claim(pub.id, pub.version,
        now_epoch=pub.scheduled_epoch, owner_user_id=123, channel_id=-100999)
    rows = PartReceiptStore(services.conn).parts_of_attempt(attempt)
    assert len(rows) == 1 and rows[0].snapshot == plan


@pytest.mark.parametrize("source_ids,result", [((11,), (301,)),
    ((11, 12, 13), (303, 301, 302))])
def test_native_one_call_all_ids_durable_before_completion(services, monkeypatch,
                                                         source_ids, result):
    pub, _ = native_publication(services, source_ids)
    api = FakeAPI(services, result)
    original = services.dispatch.complete

    def complete(pub_id, attempt_id, ids, permalink, **kwargs):
        row = PartReceiptStore(services.conn).parts_of_attempt(attempt_id)[0]
        assert row.state == "confirmed"
        assert row.channel_message_ids == result  # destination order preserved
        return original(pub_id, attempt_id, ids, permalink, **kwargs)

    monkeypatch.setattr(services.dispatch, "complete", complete)
    service = sender(services, api, owner_user_id=123, channel_id=-100999)
    outcome = send(service, pub)
    assert outcome[0].outcome == "published"
    assert api.calls == [{"chat_id": -100999, "from_chat_id": 123,
                          "message_ids": list(source_ids)}]
    assert not api.notices
    history = services.conn.execute("SELECT * FROM published_history").fetchone()
    assert json.loads(history["channel_message_ids"]) == sorted(result)
    assert history["permalink"].endswith(f"/{max(result)}")
    assert services.conn.execute("SELECT COUNT(*) FROM navigation_edits").fetchone()[0] == 0
    assert send(service, pub) == []


@pytest.mark.parametrize("ack", [(301,), (301, 302, 303, 304),
    (301, 301, 302), (True, 302, 303), (0, 302, 303),
    (-1, 302, 303), ("301", 302, 303), (None, 302, 303), (), None])
def test_bad_ack_is_uncertain_and_never_retried(services, ack):
    pub, _ = native_publication(services)
    api = FakeAPI(services, ack)
    service = sender(services, api, owner_user_id=123, channel_id=-100999)
    outcome = send(service, pub)
    assert outcome[0].outcome == "uncertain"
    row = services.conn.execute("SELECT * FROM publication_parts").fetchone()
    assert row["state"] == "uncertain" and row["channel_message_ids"] is None
    services.dispatch.recovery_sweep(now_epoch=pub.scheduled_epoch)
    restarted = sender(services, api, owner_user_id=123, channel_id=-100999)
    assert send(restarted, pub) == []
    assert len(api.calls) == 1 and api.notices


@pytest.mark.parametrize("binding", [{}, {"owner_user_id": 123},
    {"owner_user_id": 124, "channel_id": -100999},
    {"owner_user_id": 123, "channel_id": -100998}])
def test_sender_binding_refusal_leaves_no_attempt_or_call(services, binding):
    pub, _ = native_publication(services)
    api = FakeAPI(services)
    assert send(sender(services, api, **binding), pub) == []
    assert not api.calls and not services.dispatch.unfinished_attempts()
    assert services.publications.get(pub.id).status.value == "scheduled"


@pytest.mark.parametrize("error,status,part_state", [
    (TimeoutError(), "uncertain", "uncertain"),
    (TelegramBadRequest(method=ForwardMessages(chat_id=-100999,
        from_chat_id=123, message_ids=[11]), message="source unavailable"),
     "missed", "failed")])
def test_failure_parks_without_fallback_and_warns(services, error, status, part_state):
    pub, _ = native_publication(services)
    api = FakeAPI(services, error=error)
    assert send(sender(services, api, owner_user_id=123,
                       channel_id=-100999), pub)[0].outcome == status
    assert services.conn.execute("SELECT state FROM publication_parts").fetchone()[0] == part_state
    assert api.notices and len(api.calls) == 1


def test_crash_after_ack_receipt_survives_recovery_no_resend(services, monkeypatch):
    pub, _ = native_publication(services)
    api = FakeAPI(services)
    def crash(*args, **kwargs):
        raise RuntimeError("simulated process loss")
    monkeypatch.setattr(services.dispatch, "complete", crash)
    with pytest.raises(RuntimeError):
        send(sender(services, api, owner_user_id=123, channel_id=-100999), pub)
    assert services.conn.execute("SELECT state FROM publication_parts").fetchone()[0] == "confirmed"
    services.dispatch.recovery_sweep(now_epoch=pub.scheduled_epoch)
    assert send(sender(services, api, owner_user_id=123, channel_id=-100999), pub) == []
    assert len(api.calls) == 1


def test_receipt_storage_refuses_wrong_native_count(services):
    pub, _ = native_publication(services)
    attempt = services.dispatch.claim(pub.id, pub.version,
        now_epoch=pub.scheduled_epoch, owner_user_id=123, channel_id=-100999)
    receipts = PartReceiptStore(services.conn)
    with pytest.raises(TransitionError):
        receipts.record_confirmed(attempt, 1, (301,), "https://t.me/c/999/301")
    assert receipts.parts_of_attempt(attempt)[0].state == "pending"


def test_sender_stale_claim_makes_no_outbound_call(services, monkeypatch):
    pub, _ = native_publication(services)
    original = services.dispatch.claim
    def stale(pub_id, version, **kwargs):
        return original(pub_id, version + 1, **kwargs)
    monkeypatch.setattr(services.dispatch, "claim", stale)
    api = FakeAPI(services)
    assert send(sender(services, api, owner_user_id=123, channel_id=-100999), pub) == []
    assert not api.calls and not services.dispatch.unfinished_attempts()


def test_sender_rechecks_durable_intent_binding_before_api(services, monkeypatch):
    pub, plan = native_publication(services)
    original = services.dispatch.mark_in_progress
    def tamper(attempt, **kwargs):
        original(attempt, **kwargs)
        data = json.loads(plan.to_json())
        data["binding"]["channel_id"] = -100998
        services.conn.execute("UPDATE publication_parts SET part_snapshot_json=?",
                              (json.dumps(data),))
    monkeypatch.setattr(services.dispatch, "mark_in_progress", tamper)
    api = FakeAPI(services)
    with pytest.raises(ContentValidationError):
        send(sender(services, api, owner_user_id=123, channel_id=-100999), pub)
    assert not api.calls
    services.dispatch.recovery_sweep(now_epoch=pub.scheduled_epoch)
    assert services.publications.get(pub.id).status.value == "uncertain"


@pytest.mark.parametrize("ids", [(301,), (301, 302, 303, 304),
    (301, 301, 302), (True, 302, 303), ("301", 302, 303), (0, 302, 303)])
def test_sender_does_not_trust_an_adapter_claiming_invalid_sent(services, ids):
    pub, _ = native_publication(services)
    api = FakeAPI(services)
    service = sender(services, api, owner_user_id=123, channel_id=-100999)
    async def invalid_publish(content):
        return Sent(message_ids=ids, permalink="https://t.me/c/999/301")
    service._transport.publish = invalid_publish
    assert send(service, pub)[0].outcome == "uncertain"
    assert services.conn.execute("SELECT state FROM publication_parts").fetchone()[0] == "uncertain"
