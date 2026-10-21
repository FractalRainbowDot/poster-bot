"""Staged content collection for one post-in-progress.

Rules encoded here (PLAN §2):
- one post is text OR one media OR one album; new content never silently
  overwrites staged content — the flow must obtain an explicit replace;
- incoming album items are recognized by media_group_id and buffered until
  the owner presses Continue; the buffer is JSON-durable (survives restart);
- album items must be photo/video; audio/service/unsupported items inside an
  album group are ignored (the flow explains);
- a forwarded channel message defaults to publishing the source as-is
  ('forward'); building an editable local copy is a separate explicit action;
- a fresh text while media is staged can only become the caption through an
  explicit caption action;
- all entities (custom emoji, language, user mentions) are carried verbatim;
  nothing is truncated: oversized payloads raise domain validation with an
  explanation at build time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.content import (
    ALBUM_MAX,
    Content,
    Entity,
    ForwardSource,
    MediaRef,
)

ALBUM_ITEM_KINDS = ("photo", "video")


def entities_to_json(entities: tuple[Entity, ...]) -> list[dict[str, Any]]:
    return [e.to_json() for e in entities]


def entities_from_json(data: list[dict[str, Any]]) -> tuple[Entity, ...]:
    return tuple(Entity.from_json(e) for e in data if isinstance(e, dict))


@dataclass
class Staged:
    """One content-in-progress. Exactly one branch is active at a time
    (kind None = empty)."""

    kind: str | None = None  # text | media | album | forward
    # text branch
    text: str | None = None
    entities: tuple[Entity, ...] = ()
    # single media branch
    media: MediaRef | None = None
    # album branch (buffered until Continue)
    album_group_id: str | None = None
    album_items: list[MediaRef] = field(default_factory=list)
    album_caption: str | None = None
    album_caption_entities: tuple[Entity, ...] = ()
    ignored_group_items: int = 0
    # forward branch: source as-is by default, local copy parts kept separately
    forward_chat_id: int | None = None
    forward_ids: list[int] = field(default_factory=list)
    forward_group_id: str | None = None
    copy: "Staged | None" = None  # editable-copy candidate (media/album/text)

    @property
    def empty(self) -> bool:
        return self.kind is None

    # ---- intake results ---------------------------------------------------
    def add_text(self, text: str, entities: tuple[Entity, ...]) -> str:
        """Returns 'staged' | 'caption_possible' | 'conflict'."""
        if self.empty:
            self.kind, self.text, self.entities = "text", text, entities
            return "staged"
        if self.kind in ("media", "album") and self.text is None:
            return "caption_possible"
        return "conflict"

    def add_media(self, kind: str, file_id: str, caption: str | None,
                  caption_entities: tuple[Entity, ...],
                  media_group_id: str | None) -> str:
        """Returns 'staged' | 'album_extended' | 'ignored_in_album' | 'conflict'."""
        if media_group_id is not None:
            if self.empty:
                self.kind = "album"
                self.album_group_id = media_group_id
            elif self.kind != "album" or self.album_group_id != media_group_id:
                return "conflict"
            if kind not in ALBUM_ITEM_KINDS:
                self.ignored_group_items += 1
                return "ignored_in_album"
            if len(self.album_items) >= ALBUM_MAX:
                return "conflict"  # oversize albums are rejected, not split
            # per-item captions survive intake verbatim; Telegram renders the
            # first item's caption as the album caption but carries every
            # item's own caption through the payload, so nothing may drop it
            self.album_items.append(
                MediaRef(
                    kind=kind, file_id=file_id, caption=caption,
                    caption_entities=caption_entities,
                )
            )
            return "album_extended"
        if not self.empty:
            return "conflict"
        self.kind = "media"
        self.media = MediaRef(
            kind=kind, file_id=file_id, caption=caption,
            caption_entities=caption_entities,
        )
        return "staged"

    def add_forward(self, chat_id: int, message_id: int,
                    media_group_id: str | None,
                    copy_candidate: "Staged") -> str:
        """Returns 'staged' | 'forward_extended' | 'conflict'."""
        if self.empty:
            self.kind = "forward"
            self.forward_chat_id = chat_id
            self.forward_ids = [message_id]
            self.forward_group_id = media_group_id
            self.copy = copy_candidate
            return "staged"
        if (
            self.kind == "forward"
            and self.forward_chat_id == chat_id
            and media_group_id is not None
            and self.forward_group_id in (None, media_group_id)
        ):
            self.forward_group_id = media_group_id
            self.forward_ids.append(message_id)
            if self.copy is not None:
                if copy_candidate.kind == "media" and copy_candidate.media:
                    self.copy.add_media(
                        copy_candidate.media.kind,
                        copy_candidate.media.file_id,
                        copy_candidate.media.caption,
                        copy_candidate.media.caption_entities,
                        media_group_id,
                    )
                elif copy_candidate.kind == "album":
                    for item in copy_candidate.album_items:
                        if len(self.copy.album_items) < ALBUM_MAX:
                            self.copy.add_media(
                                item.kind, item.file_id, item.caption,
                                item.caption_entities, media_group_id
                            )
            return "forward_extended"
        return "conflict"

    def set_caption(self, text: str, entities: tuple[Entity, ...]) -> str:
        """Explicit caption action (media/album only).

        Returns 'set' when the caption was applied, 'ambiguous' when the
        album already carries per-item captions (silently overwriting them
        with an album-level caption is never allowed), and 'none' when this
        branch cannot take a caption at all."""
        if self.kind == "media" and self.media is not None:
            self.media = MediaRef(
                kind=self.media.kind, file_id=self.media.file_id,
                caption=text, caption_entities=entities,
            )
            return "set"
        if self.kind == "album":
            if any(item.caption is not None for item in self.album_items):
                return "ambiguous"
            self.album_caption, self.album_caption_entities = text, entities
            return "set"
        if self.kind == "forward" and self.copy is not None:
            return self.copy.set_caption(text, entities)
        return "none"

    def as_copy(self) -> "Staged":
        """Explicit 'make editable copy': the forward branch collapses to the
        locally-stored copy parts."""
        assert self.kind == "forward" and self.copy is not None
        return self.copy

    def build(self) -> Content:
        """Assemble the validated Content (oversize → ContentValidationError
        with an owner-presentable explanation)."""
        if self.kind == "text":
            return Content(kind="text", text=self.text, entities=self.entities)
        if self.kind == "media" and self.media is not None:
            return Content(kind=self.media.kind, media=self.media)
        if self.kind == "album":
            return Content(
                kind="album",
                items=tuple(self.album_items),
                caption=self.album_caption,
                caption_entities=self.album_caption_entities,
            )
        if self.kind == "forward":
            return Content(
                kind="forward",
                mode="forward",
                forward=ForwardSource(
                    chat_id=int(self.forward_chat_id or 0),
                    message_ids=tuple(self.forward_ids),
                ),
            )
        raise ValueError("nothing staged")

    def build_copy(self) -> Content:
        assert self.copy is not None
        return self.copy.build()

    def summary(self) -> tuple[str, int]:
        """Russian shape label + item/message count for list/summary lines."""
        if self.kind == "text":
            return "Текст", len(self.text or "")
        if self.kind == "media" and self.media is not None:
            return {"photo": "Фото", "video": "Видео",
                    "audio": "Аудио (mp3)"}[self.media.kind], 1
        if self.kind == "album":
            return "Альбом", len(self.album_items)
        if self.kind == "forward":
            return "Пересылка источника", len(self.forward_ids)
        return "Пусто", 0

    # ---- copy-merge helpers (forward branch) --------------------------------
    def _single_kind(self) -> str | None:
        return self.media.kind if self.kind == "media" and self.media else None

    def _single_file_id(self) -> str | None:
        return self.media.file_id if self.kind == "media" and self.media else None

    # ---- JSON persistence (durable across restarts) -------------------------
    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind}
        if self.text is not None:
            data["text"] = self.text
            data["entities"] = entities_to_json(self.entities)
        if self.media is not None:
            data["media"] = self.media.to_json()
        if self.kind == "album":
            data["album_group_id"] = self.album_group_id
            data["album_items"] = [m.to_json() for m in self.album_items]
            if self.album_caption is not None:
                data["album_caption"] = self.album_caption
                data["album_caption_entities"] = entities_to_json(
                    self.album_caption_entities
                )
            data["ignored_group_items"] = self.ignored_group_items
        if self.kind == "forward":
            data["forward_chat_id"] = self.forward_chat_id
            data["forward_ids"] = list(self.forward_ids)
            data["forward_group_id"] = self.forward_group_id
            data["copy"] = self.copy.to_dict() if self.copy else None
        return data

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Staged":
        staged = Staged(kind=data.get("kind"))
        staged.text = data.get("text")
        staged.entities = entities_from_json(data.get("entities", []))
        if isinstance(data.get("media"), dict):
            staged.media = MediaRef.from_json(data["media"])
        staged.album_group_id = data.get("album_group_id")
        staged.album_items = [
            MediaRef.from_json(m) for m in data.get("album_items", [])
            if isinstance(m, dict)
        ]
        staged.album_caption = data.get("album_caption")
        staged.album_caption_entities = entities_from_json(
            data.get("album_caption_entities", [])
        )
        staged.ignored_group_items = int(data.get("ignored_group_items", 0))
        staged.forward_chat_id = data.get("forward_chat_id")
        staged.forward_ids = list(data.get("forward_ids", []))
        staged.forward_group_id = data.get("forward_group_id")
        copy_data = data.get("copy")
        staged.copy = Staged.from_dict(copy_data) if isinstance(copy_data, dict) else None
        return staged


def copy_candidate_from_media(kind: str, file_id: str, caption: str | None,
                              caption_entities: tuple[Entity, ...],
                              media_group_id: str | None) -> Staged:
    """Local editable-copy candidate built from a received (forwarded) copy."""
    staged = Staged()
    staged.add_media(kind, file_id, caption, caption_entities, media_group_id)
    return staged


def copy_candidate_from_text(text: str,
                             entities: tuple[Entity, ...]) -> Staged:
    staged = Staged()
    staged.add_text(text, entities)
    return staged