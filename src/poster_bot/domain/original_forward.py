"""Explicit unsigned native-forward snapshots, separate from signed copies.

No transport or storage dependency. Intake must supply actual owner-private-chat
message coordinates, not forward_origin metadata. The accepted-message factory
accounts for the complete intake batch; it never sorts, deduplicates or truncates.
Persisted grouping metadata records that intake decision, not remote availability.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, TypeAlias

from .content import ALBUM_MAX, Content, ForwardSource
from .errors import ContentValidationError
from .planner import LogicalPost, PlannedPart, PublicationPlan

ORIGINAL_FORWARD_MODE = "original_forward"


def _integer(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or (value <= 0 if positive else value == 0):
        raise ContentValidationError(f"{name} must be a {'positive' if positive else 'non-zero'} integer")
    return value


def _object(data: object, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(data, dict) or not required <= data.keys() or data.keys() - (required | (optional or set())):
        raise ContentValidationError("snapshot has missing or unknown fields")
    return data


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContentValidationError("snapshot has duplicate fields")
        result[key] = value
    return result


def _load(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ContentValidationError("snapshot must be JSON text")
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise ContentValidationError("snapshot is not valid JSON") from None
    if not isinstance(data, dict):
        raise ContentValidationError("snapshot must be a JSON object")
    return data


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class OriginalForwardSource:
    chat_id: int
    message_ids: tuple[int, ...]
    media_group_id: str | None = None

    def __post_init__(self) -> None:
        _integer(self.chat_id, "source.chat_id", positive=True)
        if type(self.message_ids) is not tuple or not 1 <= len(self.message_ids) <= ALBUM_MAX:
            raise ContentValidationError("source needs 1..10 immutable message ids; no truncation")
        for message_id in self.message_ids:
            _integer(message_id, "source.message_id", positive=True)
        if any(a >= b for a, b in zip(self.message_ids, self.message_ids[1:])):
            raise ContentValidationError("source message ids must be unique and strictly ascending")
        if self.media_group_id is not None and (type(self.media_group_id) is not str or not self.media_group_id):
            raise ContentValidationError("source.media_group_id must be a non-empty string")
        if len(self.message_ids) > 1 and self.media_group_id is None:
            raise ContentValidationError("multiple messages require an accepted media group")

    def to_json_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"chat_id": self.chat_id, "message_ids": list(self.message_ids)}
        if self.media_group_id is not None:
            data["media_group_id"] = self.media_group_id
        return data

    @staticmethod
    def from_json_dict(data: object) -> OriginalForwardSource:
        data = _object(data, {"chat_id", "message_ids"}, {"media_group_id"})
        if type(data["message_ids"]) is not list:
            raise ContentValidationError("source.message_ids must be a list")
        if "media_group_id" in data and data["media_group_id"] is None:
            raise ContentValidationError("source.media_group_id cannot be null")
        return OriginalForwardSource(data["chat_id"], tuple(data["message_ids"]), data.get("media_group_id"))


def _native_data(raw: str, version: int, *, binding: bool) -> dict[str, Any]:
    data = _object(_load(raw), {"v", "mode", "source"} | ({"binding"} if binding else set()))
    if type(data["v"]) is not int or data["v"] != version or data["mode"] != ORIGINAL_FORWARD_MODE:
        raise ContentValidationError("unsupported native-forward snapshot version or mode")
    return data


@dataclass(frozen=True)
class OriginalForwardInput:
    source: OriginalForwardSource

    def __post_init__(self) -> None:
        if type(self.source) is not OriginalForwardSource:
            raise ContentValidationError("native input requires a validated original source")

    def to_json(self) -> str:
        return _dump({"v": 3, "mode": ORIGINAL_FORWARD_MODE, "source": self.source.to_json_dict()})

    @staticmethod
    def from_json(raw: str) -> OriginalForwardInput:
        data = _native_data(raw, 3, binding=False)
        return OriginalForwardInput(OriginalForwardSource.from_json_dict(data["source"]))

    @staticmethod
    def from_accepted_messages(
        owner_user_id: int, messages: tuple[tuple[int, int, str | None], ...],
    ) -> OriginalForwardInput:
        """Take EVERY actual accepted message (chat, id, actual media_group_id).

        Caller owns album-settling/completeness at intake. Passing a filtered
        prefix defeats that boundary; this factory intentionally never filters.
        """
        _integer(owner_user_id, "owner_user_id", positive=True)
        if type(messages) is not tuple or not messages:
            raise ContentValidationError("accepted messages must be a complete immutable batch")
        if any(type(m) is not tuple or len(m) != 3 for m in messages):
            raise ContentValidationError("accepted message coordinates are malformed")
        for chat, message_id, group in messages:
            _integer(chat, "accepted chat", positive=True)
            _integer(message_id, "accepted message id", positive=True)
            if chat != owner_user_id or group != messages[0][2]:
                raise ContentValidationError("accepted messages must share the owner's actual chat and media group")
        return OriginalForwardInput(OriginalForwardSource(
            owner_user_id, tuple(m[1] for m in messages), messages[0][2]))


@dataclass(frozen=True)
class OriginalForwardPlan:
    source: OriginalForwardSource
    owner_user_id: int
    channel_id: int

    def __post_init__(self) -> None:
        if type(self.source) is not OriginalForwardSource:
            raise ContentValidationError("native plan requires a validated original source")
        _integer(self.owner_user_id, "binding.owner_user_id", positive=True)
        _integer(self.channel_id, "binding.channel_id")
        if self.source.chat_id != self.owner_user_id:
            raise ContentValidationError("source must be the owner's private chat")

    @property
    def version(self) -> int:
        return 4

    def validate_binding(self, *, owner_user_id: int, channel_id: int) -> None:
        _integer(owner_user_id, "owner_user_id", positive=True)
        _integer(channel_id, "channel_id")
        if (owner_user_id, channel_id) != (self.owner_user_id, self.channel_id):
            raise ContentValidationError("native-forward owner or destination binding mismatch")

    def to_json(self) -> str:
        return _dump({"v": self.version, "mode": ORIGINAL_FORWARD_MODE,
                      "source": self.source.to_json_dict(),
                      "binding": {"owner_user_id": self.owner_user_id, "channel_id": self.channel_id}})

    @staticmethod
    def from_json(raw: str) -> OriginalForwardPlan:
        data = _native_data(raw, 4, binding=True)
        binding = _object(data["binding"], {"owner_user_id", "channel_id"})
        return OriginalForwardPlan(OriginalForwardSource.from_json_dict(data["source"]),
                                   binding["owner_user_id"], binding["channel_id"])


# A native album is ONE API operation, not N signed PlannedParts. Persist the
# complete plan as its intent snapshot so source and binding cannot drift.
OriginalForwardIntent: TypeAlias = OriginalForwardPlan
PublicationInput: TypeAlias = LogicalPost | OriginalForwardInput
FrozenPublicationPlan: TypeAlias = PublicationPlan | OriginalForwardPlan
SendSnapshot: TypeAlias = PlannedPart | OriginalForwardIntent


def build_original_forward_plan(
    logical: OriginalForwardInput, *, owner_user_id: int, channel_id: int,
) -> OriginalForwardPlan:
    if type(logical) is not OriginalForwardInput:
        raise ContentValidationError("native planning requires original-forward input")
    return OriginalForwardPlan(logical.source, owner_user_id, channel_id)


def parse_publication_input(raw: str) -> PublicationInput:
    data = _load(raw)
    version = data.get("v")
    if type(version) is int and version == 3 and data.get("mode") == ORIGINAL_FORWARD_MODE:
        return OriginalForwardInput.from_json(raw)
    if type(version) is int and version == 2 and "mode" not in data:
        _object(data, {"v"}, {"body", "entities", "media"})
        return LogicalPost.from_json(raw)
    raise ContentValidationError("unsupported publication input version or mode")


def parse_publication_plan(raw: str) -> FrozenPublicationPlan:
    data = _load(raw)
    version = data.get("v")
    if type(version) is int and version == 4 and data.get("mode") == ORIGINAL_FORWARD_MODE:
        return OriginalForwardPlan.from_json(raw)
    if type(version) is int and version in (2, 3) and "mode" not in data:
        _object(data, {"v", "parts", "signature"}, {"diagnostics"})
        return PublicationPlan.from_json(raw)
    raise ContentValidationError("unsupported publication plan version or mode")


def parse_send_snapshot(raw: str) -> SendSnapshot:
    data = _load(raw)
    if type(data.get("v")) is int and data["v"] == 4 and data.get("mode") == ORIGINAL_FORWARD_MODE:
        return OriginalForwardPlan.from_json(raw)
    if "v" not in data and "mode" not in data:
        _object(data, {"index", "body_slice", "content_json", "footer"})
        return PlannedPart.from_json(data)
    raise ContentValidationError("unsupported send snapshot version or mode")


def plan_primary_content(plan: FrozenPublicationPlan) -> Content:
    """Legacy posts.content_json projection, with ALL original coordinates."""
    if type(plan) is OriginalForwardPlan:
        return Content(kind="forward", mode="forward", forward=ForwardSource(
            plan.source.chat_id, plan.source.message_ids))
    if isinstance(plan, PublicationPlan):
        return plan.parts[0].content
    raise ContentValidationError("unsupported publication plan type")
