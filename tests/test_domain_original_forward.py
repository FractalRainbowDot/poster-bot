"""Native-forward snapshots are separate from mandatory-signature plans."""
import json
from dataclasses import FrozenInstanceError

import pytest

from poster_bot.domain.errors import ContentValidationError
from poster_bot.domain.original_forward import (
    OriginalForwardInput, OriginalForwardPlan, OriginalForwardSource,
    build_original_forward_plan, parse_publication_input,
    parse_publication_plan, parse_send_snapshot, plan_primary_content,
)
from poster_bot.repo.plans import validate_plan_pair


def native_pair(ids=(11,), group=None):
    logical = OriginalForwardInput(OriginalForwardSource(42, ids, group))
    return logical, build_original_forward_plan(logical, owner_user_id=42, channel_id=-10077)


def test_native_roundtrip_and_single_operation_snapshot():
    logical, plan = native_pair((11, 12), "accepted-album")
    assert parse_publication_input(logical.to_json()) == logical
    assert parse_publication_plan(plan.to_json()) == plan
    assert parse_send_snapshot(plan.to_json()) == plan
    assert set(json.loads(plan.to_json())) == {"v", "mode", "source", "binding"}
    assert json.loads(plan.to_json())["v"] == 4
    assert json.loads(logical.to_json())["v"] == 3
    assert plan_primary_content(plan).forward.message_ids == (11, 12)
    validate_plan_pair(logical, plan)
    with pytest.raises(FrozenInstanceError):
        plan.channel_id = -10088


@pytest.mark.parametrize("ids", [(), (True,), (0,), (-1,), (2, 2), (2, 1), tuple(range(1, 12)), [1]])
def test_invalid_source_ids(ids):
    with pytest.raises(ContentValidationError):
        native_pair(ids, "group")


@pytest.mark.parametrize("chat", [True, 0, -42, "42"])
def test_source_requires_owner_private_chat(chat):
    with pytest.raises(ContentValidationError):
        OriginalForwardSource(chat, (1,))


def test_multiple_sources_require_group_and_accepted_accounting():
    with pytest.raises(ContentValidationError):
        native_pair((1, 2))
    for messages in [((42, 1, "a"), (42, 2, "b")),
                     ((42, 1, "a"), (43, 2, "a")),
                     ((42, 2, "a"), (42, 1, "a")),
                     tuple((42, n, "a") for n in range(1, 12))]:
        with pytest.raises(ContentValidationError):
            OriginalForwardInput.from_accepted_messages(42, messages)
    accepted = OriginalForwardInput.from_accepted_messages(42, ((42, 1, "a"), (42, 2, "a")))
    assert accepted.source.message_ids == (1, 2)
    assert accepted.source.media_group_id == "a"


@pytest.mark.parametrize("field,value", [("owner_user_id", True), ("owner_user_id", 43),
                                          ("channel_id", True), ("channel_id", "@name"),
                                          ("channel_id", 0)])
def test_binding_validation(field, value):
    logical, _ = native_pair()
    kwargs = {"owner_user_id": 42, "channel_id": -10077, field: value}
    with pytest.raises(ContentValidationError):
        build_original_forward_plan(logical, **kwargs)


def test_binding_context_and_pair_source_exactness():
    logical, plan = native_pair()
    plan.validate_binding(owner_user_id=42, channel_id=-10077)
    for kwargs in ({"owner_user_id": 43, "channel_id": -10077},
                   {"owner_user_id": 42, "channel_id": -10078},
                   {"owner_user_id": True, "channel_id": -10077}):
        with pytest.raises(ContentValidationError):
            plan.validate_binding(**kwargs)
    other, _ = native_pair((12,))
    with pytest.raises(ContentValidationError):
        validate_plan_pair(other, plan)


@pytest.mark.parametrize("parser,which", [(parse_publication_input, 0), (parse_publication_plan, 1)])
@pytest.mark.parametrize("mutation", ["extra", "body", "signature", "parts", "source-extra", "wrong-mode", "bool-version", "unsupported", "duplicate-key", "group-null", "ids-bool"])
def test_strict_discriminated_native_parser(parser, which, mutation):
    data = json.loads(native_pair()[which].to_json())
    if mutation in {"extra", "body", "signature", "parts"}:
        data[mutation] = []
    elif mutation == "source-extra":
        data["source"]["unexpected"] = True
    elif mutation == "wrong-mode":
        data["mode"] = "copy"
    elif mutation == "bool-version":
        data["v"] = True
    elif mutation == "unsupported":
        data["v"] = 99
    elif mutation == "group-null":
        data["source"]["media_group_id"] = None
    elif mutation == "ids-bool":
        data["source"]["message_ids"] = [True]
    raw = json.dumps(data)
    if mutation == "duplicate-key":
        raw = raw[:-1] + ', "mode": "original_forward"}'
    with pytest.raises(ContentValidationError):
        parser(raw)


def test_plan_direct_parser_rejects_input_and_mixed_binding():
    logical, plan = native_pair()
    with pytest.raises(ContentValidationError):
        OriginalForwardPlan.from_json(logical.to_json())
    data = json.loads(plan.to_json())
    data["binding"]["extra"] = 1
    with pytest.raises(ContentValidationError):
        parse_publication_plan(json.dumps(data))
