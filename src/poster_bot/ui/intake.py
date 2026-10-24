"""Planned-intake aggregation: what the owner sends is what gets published.

When a signature is configured the owner's draft moves through a planned
pipeline: every incoming chunk is *appended* to the staged collection until
Continue (never overwritten), captions arrive as ordinary paragraphs of the
aggregated body in the exact order the owner sent them, and the frozen
PublicationPlan then renders the physical stack. The planner refuses a body
combined with captioned media by design ('item_caption_conflict'), so this
module owns folding caption text into the body — with entities remapped onto
the merged UTF-16 offsets — before any LogicalPost is built.

Pure logic: no I/O, no Telegram. Used by ui/flow.py.

``normalize_content`` lives in domain.normalization (the gated sender uses
the identical pure rule to auto-prepare a legacy planless copy; preview and
delivery must run the SAME lossless fold); it stays re-exported here so the
flow's ``intake.normalize_content`` API is unchanged.
"""
from __future__ import annotations

from poster_bot.domain.content import (
    ALBUM_MAX,
    ContentValidationError,
    Entity,
    MediaRef,
    utf16_length,
)
from poster_bot.domain.normalization import (
    PARAGRAPH_SEP,
    normalize_content,
)
from poster_bot.domain.planner import LogicalPost
from poster_bot.ui.staged import Staged

__all__ = [
    "ALBUM_MAX", "PARAGRAPH_SEP", "append_chunk", "absorb_media",
    "build_logical", "fold_caption", "fold_residual", "normalize_content",
    "split_paragraph_sizes",
    "forward_media_candidate", "candidate_logical", "merge_forward_candidates",
    "staged_from_logical",
]

_SEP_U16 = 2  # two BMP code units


def _append_body(staged: Staged, text: str,
                 entities: tuple[Entity, ...]) -> None:
    """Append one owner paragraph to the aggregated body. Earlier chunks stay
    verbatim (chunk 3 never replaces chunk 2); the new chunk's entity ranges
    shift by the existing UTF-16 length plus the separator. Nothing is
    truncated: a LogicalPost body may exceed one message — the planner splits
    it or asks the owner to split manually."""
    shifted: list[Entity] = []
    base = utf16_length(staged.text or "")
    if base:
        base += _SEP_U16
    for ent in entities:
        shifted.append(Entity(type=ent.type, offset=ent.offset + base,
                              length=ent.length, url=ent.url,
                              user_id=ent.user_id, username=ent.username,
                              custom_emoji_id=ent.custom_emoji_id,
                              language=ent.language))
    staged.text = text if not staged.text else f"{staged.text}{PARAGRAPH_SEP}{text}"
    staged.entities = tuple(staged.entities) + tuple(shifted)


def append_chunk(staged: Staged, text: str,
                 entities: tuple[Entity, ...] = ()) -> None:
    """Intake one text message in planned mode: first text starts the body,
    every later one appends a paragraph. Staged kind becomes/stays 'text'
    only when no media is staged yet; with media staged the text still joins
    the body — media keeps its own branch."""
    if staged.kind is None:
        staged.kind = "text"
    if staged.kind == "forward":
        raise ContentValidationError("для пересланного сообщения сначала "
                                     "выберите действие")
    _append_body(staged, text, entities)


def fold_caption(staged: Staged, caption: str,
                 entities: tuple[Entity, ...] = ()) -> None:
    """A media caption becomes a body paragraph at the position the owner
    sent it (chronological intake order), so the planner never sees a body
    combined with item captions. A present-but-blank-looking caption is still
    owner text: presence is a None/empty check, never a stripped comparison,
    and the caption is appended verbatim so entity offsets stay valid."""
    if not caption:
        return
    _append_body(staged, caption, entities)


def absorb_media(staged: Staged, kind: str, file_id: str, caption: str | None,
                 caption_entities: tuple[Entity, ...],
                 media_group_id: str | None) -> str:
    """Planned-mode media intake: everything photo/video joins ONE ordered
    album, an audio/video-file solo stays a single item, and an 11th album
    item is REFUSED without touching the durable first ten (the flow warns
    and does not capture the extra one).

    Returns 'ok' | 'overflow' | 'unsupported'. Never mutates on refusal.
    """
    if kind == "audio":
        if staged.kind is not None and staged.kind != "text":
            return "unsupported"
        existing = _current_media(staged)
        if existing:
            return "unsupported"  # audio beside photos/videos: owner decides
        fold_caption(staged, caption or "", caption_entities)
        if staged.kind is None:
            staged.kind = "media"
        staged.media = MediaRef(kind="audio", file_id=file_id)
        return "ok"
    if kind not in ("photo", "video"):
        return "unsupported"
    media = _current_media(staged)
    if len(media) >= ALBUM_MAX:
        return "overflow"
    fold_caption(staged, caption or "", caption_entities)
    item = MediaRef(kind=kind, file_id=file_id)
    if staged.kind is None or staged.kind == "text":
        staged.kind = "album"
        staged.album_group_id = media_group_id or "staged-album"
        staged.album_items = list(media) + [item]
        staged.media = None
        return "ok"
    if staged.kind == "media":
        if staged.media is not None and staged.media.kind == "audio":
            return "unsupported"  # audio does not join a photo/video album
        # a second item (or a different album arriving beside a solo photo)
        # promotes everything into one ordered album, arrival order preserved
        promoted = [staged.media, item] if staged.media else [item]
        staged.kind = "album"
        staged.album_group_id = media_group_id or "staged-album"
        staged.album_items = promoted
        staged.media = None
        return "ok"
    if staged.kind == "album":
        # a second source album folds into the already staged one: the owner
        # composed one logical album; physical send uses one media group
        staged.album_items = list(staged.album_items) + [item]
        return "ok"
    return "unsupported"  # forward branch: copy flow owns conversion


def _current_media(staged: Staged) -> list[MediaRef]:
    if staged.kind == "album":
        return list(staged.album_items)
    if staged.kind == "media" and staged.media is not None:
        return [staged.media]
    return []


def build_logical(staged: Staged) -> LogicalPost:
    """Snapshot staged content into planner input. Captions were folded at
    intake; any caption still riding on media (older staged payloads) is
    folded here so the planner's conflict guard never fires on owner data."""
    fold_residual(staged)
    body = staged.text or None
    media = tuple(_current_media(staged))
    if body is None and not media:
        raise ContentValidationError("пост пуст")
    return LogicalPost(body=body, entities=tuple(staged.entities),
                       media=media)


def fold_residual(staged: Staged) -> None:
    """Fold any media/album caption still present into the body and CLEAR it
    on the media (planner contract: item captions must be empty)."""
    if staged.kind == "media" and staged.media and staged.media.caption:
        caption, ents = staged.media.caption, staged.media.caption_entities
        staged.media = MediaRef(kind=staged.media.kind,
                                file_id=staged.media.file_id)
        fold_caption(staged, caption, ents)
    if staged.kind != "album":
        return
    if staged.album_caption:
        caption, ents = staged.album_caption, staged.album_caption_entities
        staged.album_caption, staged.album_caption_entities = None, ()
        fold_caption(staged, caption, ents)
    cleared: list[MediaRef] = []
    for item in staged.album_items:
        if item.caption:
            fold_caption(staged, item.caption, item.caption_entities)
            cleared.append(MediaRef(kind=item.kind, file_id=item.file_id))
        else:
            cleared.append(item)
    staged.album_items = cleared


def split_paragraph_sizes(logical: LogicalPost) -> list[tuple[int, int]]:
    """(1-based paragraph number, char length) for every paragraph of the
    body — shown when the planner demands a manual split. Only sizes travel
    to the owner, never raw content."""
    if not logical.body:
        return []
    return [(i, len(para)) for i, para in
            enumerate(logical.body.split(PARAGRAPH_SEP), start=1)
            if para.strip()]


def forward_media_candidate(kind: str, file_id: str, caption: str | None,
                            entities: tuple[Entity, ...],
                            media_group_id: str | None) -> Staged:
    """Validate incoming text/entities without applying OUTBOUND caption limits.

    Unsupported grouped media stays an incomplete candidate, just as on the
    legacy path: it must never pass the full-source copy accounting guard.
    """
    if media_group_id is not None and kind not in ("photo", "video"):
        # Keep the original forward coordinates, but no usable local item.
        # Full-source accounting at consent must reject this partial copy.
        return Staged(kind="album", album_group_id=media_group_id,
                      ignored_group_items=1)
    logical = LogicalPost(body=caption or None, entities=entities,
                          media=(MediaRef(kind=kind, file_id=file_id),))
    return Staged(kind="logical", logical_copy=logical)


def candidate_logical(candidate: Staged) -> LogicalPost:
    if candidate.logical_copy is not None:
        return candidate.logical_copy
    # Legacy candidates must still prove they are valid complete Content.
    return normalize_content(candidate.build())


def merge_forward_candidates(first: Staged, incoming: Staged) -> Staged:
    def snapshot(candidate):
        if (candidate.kind == "album" and not candidate.album_items
                and candidate.ignored_group_items):
            return None  # missing copy item; forward_ids still accounts for it
        if candidate.kind == "album" and len(candidate.album_items) == 1:
            # Upgrade compatibility: a pre-existing strict buffered album may
            # contain just its first item, before a raw logical item arrives.
            clone = Staged.from_dict(candidate.to_dict())
            return build_logical(clone)
        return candidate_logical(candidate)

    left, right = snapshot(first), snapshot(incoming)
    if left is None or right is None:
        return Staged(kind="logical", logical_copy=left or right)
    merged = staged_from_logical(left)
    if right.body:
        _append_body(merged, right.body, right.entities)
    return Staged(kind="logical", logical_copy=LogicalPost(
        body=merged.text, entities=merged.entities, media=left.media + right.media))


def staged_from_logical(logical: LogicalPost) -> Staged:
    """Consent-only conversion to the normal appendable planned intake shape."""
    staged = Staged(text=logical.body, entities=logical.entities)
    if len(logical.media) > 1:
        staged.kind = "album"
        staged.album_group_id = "forward-copy"
        staged.album_items = list(logical.media)
    elif logical.media:
        staged.kind = "media"
        staged.media = logical.media[0]
    else:
        staged.kind = "text"
    return staged
