"""Persistent post content model with Telegram limits (PLAN §6, verified).

Limits: text 1..4096; media caption 0..1024; album 2..10 items (photo/video
may mix); nothing is truncated or split: oversize content is rejected with an
explanation naming the actual length and the limit. Media travels as file_id
references reused by the same bot (no downloads are modelled here).

Two separate measures, never confused:
- text/caption length limits are counted in characters (PLAN §6 wording,
  "1-4096 characters after entities parsing"), via Python len();
- entity offsets/lengths are UTF-16 code units (Bot API convention): they
  are validated against the text's UTF-16 length and must land on character
  boundaries, so an entity can never split an astral (surrogate-pair)
  character.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .errors import ContentValidationError

TEXT_MIN, TEXT_MAX = 1, 4096
CAPTION_MAX = 1024
ALBUM_MIN, ALBUM_MAX = 2, 10

CONTENT_KINDS = ("text", "photo", "video", "audio", "album", "forward")
MODES = ("copy", "forward")
MEDIA_KINDS = ("photo", "video", "audio")
ALBUM_ITEM_KINDS = ("photo", "video")


@dataclass(frozen=True)
class Entity:
    """MessageEntity with its complete Telegram payload.

    Offsets and lengths are UTF-16 code units (Bot API convention), not
    Python character counts. Every optional field the Bot API defines is
    preserved so aiogram can rebuild an identical MessageEntity: 'user' ->
    user_id, 'text_mention' -> user_id (+ username for display),
    'custom_emoji' -> custom_emoji_id, 'pre' -> language.
    """

    type: str
    offset: int
    length: int
    url: str | None = None
    user_id: int | None = None
    username: str | None = None
    custom_emoji_id: str | None = None
    language: str | None = None

    def to_json(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "type": self.type,
            "offset": self.offset,
            "length": self.length,
        }
        if self.url is not None:
            base["url"] = self.url
        if self.user_id is not None:
            base["user_id"] = self.user_id
        if self.username is not None:
            base["username"] = self.username
        if self.custom_emoji_id is not None:
            base["custom_emoji_id"] = self.custom_emoji_id
        if self.language is not None:
            base["language"] = self.language
        return base

    @staticmethod
    def from_json(data: dict[str, Any]) -> "Entity":
        user_id = data.get("user_id")
        if user_id is not None and (
            not isinstance(user_id, int) or isinstance(user_id, bool)
        ):
            raise ContentValidationError("entity.user_id must be an integer")
        return Entity(
            type=_require_str(data.get("type"), "entity.type"),
            offset=_require_int(data.get("offset"), "entity.offset"),
            length=_require_int(data.get("length"), "entity.length"),
            url=data.get("url") if isinstance(data.get("url"), str) else None,
            user_id=user_id,
            **{
                name: data[name]
                for name in ("username", "custom_emoji_id", "language")
                if isinstance(data.get(name), str)
            },
        )


def _require_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContentValidationError(f"{field} must be a non-empty string")
    return value


def _require_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContentValidationError(f"{field} must be an integer")
    return value


def utf16_length(text: str) -> int:
    """Length in UTF-16 code units — the unit Bot API entity offsets use."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _utf16_boundaries(text: str) -> frozenset[int]:
    """UTF-16 offsets that fall on character boundaries (start, every
    code-point edge, and the total length)."""
    bounds: set[int] = {0}
    total = 0
    for ch in text:
        total += 2 if ord(ch) > 0xFFFF else 1
        bounds.add(total)
    return frozenset(bounds)


def _validate_entities(text: str, entities: tuple[Entity, ...], what: str) -> None:
    total = utf16_length(text)
    bounds = _utf16_boundaries(text)
    for i, ent in enumerate(entities):
        if ent.offset < 0 or ent.length < 1:
            raise ContentValidationError(
                f"{what} entity[{i}] has non-positive offset/length"
            )
        end = ent.offset + ent.length
        if end > total:
            raise ContentValidationError(
                f"{what} entity[{i}] spans past the text end "
                f"({ent.offset}+{ent.length} > {total} UTF-16 units)"
            )
        if ent.offset not in bounds or end not in bounds:
            raise ContentValidationError(
                f"{what} entity[{i}] range {ent.offset}..{end} is not on "
                "UTF-16 character boundaries (entity offsets are UTF-16 "
                "code units and must not split a character)"
            )


@dataclass(frozen=True)
class MediaRef:
    kind: str  # photo | video | audio
    file_id: str
    caption: str | None = None
    caption_entities: tuple[Entity, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in MEDIA_KINDS:
            raise ContentValidationError(
                f"media kind must be one of {MEDIA_KINDS}, got {self.kind!r}"
            )
        _require_str(self.file_id, "media.file_id")
        self._check_caption()

    def _check_caption(self) -> None:
        if self.caption is None:
            return
        if len(self.caption) > CAPTION_MAX:
            raise ContentValidationError(
                f"caption is {len(self.caption)} chars, limit is {CAPTION_MAX}; "
                "the post is rejected, not truncated"
            )
        _validate_entities(self.caption, self.caption_entities, "caption")

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "file_id": self.file_id}
        if self.caption is not None:
            data["caption"] = self.caption
            if self.caption_entities:
                data["caption_entities"] = [
                    e.to_json() for e in self.caption_entities
                ]
        return data

    @staticmethod
    def from_json(data: dict[str, Any]) -> "MediaRef":
        caps = data.get("caption_entities", [])
        if not isinstance(caps, list):
            raise ContentValidationError("caption_entities must be a list")
        return MediaRef(
            kind=_require_str(data.get("kind"), "media.kind"),
            file_id=_require_str(data.get("file_id"), "media.file_id"),
            caption=data["caption"] if isinstance(data.get("caption"), str) else None,
            caption_entities=tuple(
                Entity.from_json(e) for e in caps if isinstance(e, dict)
            ),
        )


@dataclass(frozen=True)
class ForwardSource:
    chat_id: int
    message_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.chat_id == 0:
            raise ContentValidationError("forward source chat_id must be non-zero")
        if not self.message_ids:
            raise ContentValidationError("forward source needs at least one message")
        if any(m <= 0 for m in self.message_ids):
            raise ContentValidationError("forward source message ids must be positive")


@dataclass(frozen=True)
class Content:
    """One post: one message or one full album, sent as a unit."""

    kind: str  # text|photo|video|audio|album|forward
    mode: str = "copy"  # copy = editable own message; forward = source as-is
    text: str | None = None
    entities: tuple[Entity, ...] = ()
    media: MediaRef | None = None
    items: tuple[MediaRef, ...] = ()  # album items (photo/video), one album caption
    caption: str | None = None  # album-level caption
    caption_entities: tuple[Entity, ...] = ()
    forward: ForwardSource | None = None

    def __post_init__(self) -> None:
        if self.kind not in CONTENT_KINDS:
            raise ContentValidationError(
                f"content kind must be one of {CONTENT_KINDS}, got {self.kind!r}"
            )
        if self.mode not in MODES:
            raise ContentValidationError(
                f"mode must be one of {MODES}, got {self.mode!r}"
            )
        if (self.mode == "forward") != (self.forward is not None):
            raise ContentValidationError(
                "forward mode requires a source and only forward mode may carry one"
            )
        validator = getattr(self, f"_check_{self.kind}")
        validator()

    def _check_forward(self) -> None:
        if any(
            v is not None
            for v in (self.text, self.media, self.caption)
        ) or self.items or self.entities or self.caption_entities:
            raise ContentValidationError(
                "forward content carries no local payload; it is sent as-is"
            )

    def _check_text(self) -> None:
        if self.media is not None or self.items or self.forward is not None:
            raise ContentValidationError("text content carries no media")
        if self.text is None:
            raise ContentValidationError("text content requires text")
        if not (TEXT_MIN <= len(self.text) <= TEXT_MAX):
            raise ContentValidationError(
                f"text is {len(self.text)} chars, limit is {TEXT_MIN}..{TEXT_MAX}; "
                "the post is rejected, not split"
            )
        _validate_entities(self.text, self.entities, "text")

    def _check_media_single(self) -> None:
        if self.text is not None or self.items or self.forward is not None:
            raise ContentValidationError(f"{self.kind} content carries only one media")
        if self.media is None or self.media.kind != self.kind:
            raise ContentValidationError(
                f"{self.kind} content requires matching media"
            )

    def _check_photo(self) -> None:
        self._check_media_single()

    def _check_video(self) -> None:
        self._check_media_single()

    def _check_audio(self) -> None:
        self._check_media_single()

    def _check_album(self) -> None:
        if self.text is not None or self.media is not None or self.forward is not None:
            raise ContentValidationError("album content carries items and one caption")
        if not (ALBUM_MIN <= len(self.items) <= ALBUM_MAX):
            raise ContentValidationError(
                f"album has {len(self.items)} items, limit is {ALBUM_MIN}..{ALBUM_MAX}; "
                "oversized albums are rejected, not split"
            )
        for i, item in enumerate(self.items):
            if item.kind not in ALBUM_ITEM_KINDS:
                raise ContentValidationError(
                    f"album item[{i}] must be photo or video, got {item.kind!r}"
                )
        if self.caption is not None and any(
            item.caption is not None for item in self.items
        ):
            raise ContentValidationError(
                "album mixes an album-level caption with per-item captions; "
                "choose one — the combination is ambiguous and is rejected, "
                "not silently overwritten"
            )
        if self.caption is not None:
            if len(self.caption) > CAPTION_MAX:
                raise ContentValidationError(
                    f"album caption is {len(self.caption)} chars, limit is "
                    f"{CAPTION_MAX}; the post is rejected, not truncated"
                )
            _validate_entities(self.caption, self.caption_entities, "album caption")

    def to_json(self) -> str:
        data: dict[str, Any] = {"kind": self.kind, "mode": self.mode}
        if self.text is not None:
            data["text"] = self.text
        if self.entities:
            data["entities"] = [e.to_json() for e in self.entities]
        if self.media is not None:
            data["media"] = self.media.to_json()
        if self.items:
            data["items"] = [m.to_json() for m in self.items]
        if self.caption is not None:
            data["caption"] = self.caption
            if self.caption_entities:
                data["caption_entities"] = [e.to_json() for e in self.caption_entities]
        if self.forward is not None:
            data["forward"] = {
                "chat_id": self.forward.chat_id,
                "message_ids": list(self.forward.message_ids),
            }
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def from_json(raw: str) -> "Content":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise ContentValidationError("content_json is not valid JSON") from None
        if not isinstance(data, dict):
            raise ContentValidationError("content_json must be a JSON object")
        items = data.get("items", [])
        ents = data.get("entities", [])
        caps = data.get("caption_entities", [])
        if not isinstance(items, list) or not isinstance(ents, list) or not isinstance(caps, list):
            raise ContentValidationError("content_json lists are malformed")
        fwd = data.get("forward")
        forward = None
        if isinstance(fwd, dict):
            msg_ids = fwd.get("message_ids", [])
            if not isinstance(msg_ids, list):
                raise ContentValidationError("forward.message_ids must be a list")
            forward = ForwardSource(
                chat_id=_require_int(fwd.get("chat_id"), "forward.chat_id"),
                message_ids=tuple(
                    _require_int(m, "forward.message_ids[]") for m in msg_ids
                ),
            )
        return Content(
            kind=_require_str(data.get("kind"), "content.kind"),
            mode=data.get("mode", "copy") if isinstance(data.get("mode", "copy"), str) else "copy",
            text=data["text"] if isinstance(data.get("text"), str) else None,
            entities=tuple(Entity.from_json(e) for e in ents if isinstance(e, dict)),
            media=MediaRef.from_json(data["media"]) if isinstance(data.get("media"), dict) else None,
            items=tuple(MediaRef.from_json(m) for m in items if isinstance(m, dict)),
            caption=data["caption"] if isinstance(data.get("caption"), str) else None,
            caption_entities=tuple(Entity.from_json(e) for e in caps if isinstance(e, dict)),
            forward=forward,
        )