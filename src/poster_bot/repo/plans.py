"""Durable versioned logical post + frozen publication plan (post_plans).

Storage contract (CHUNK2, approved brief):
- the (logical, plan) pair lives ONLY here, keyed by post_id — never inside
  posts.content_json (that column stays the FIRST physical part for legacy
  readers) and never inside ui_state (volatile conversation state);
- one row per post, replaced atomically while the composition is editable
  (draft, scheduled, suspended, missed) and frozen from the moment the
  publication is claimed: sending, uncertain or published never re-plan. An
  environment or signature-config change can therefore never mutate an
  already planned stack — the plan embeds its own validated SignatureConfig;
- every save re-validates the pair BEFORE any write: the parts must
  reconstruct the logical body losslessly and carry the logical media in
  order (an album overflow is the planner's own explicit truncation warning,
  so its 10-item prefix is the only accepted divergence) AND the stored
  snapshot must equal — canonically, character-for-character — the result of
  deterministically RECOMPUTING the pure planner on the pair itself (logical
   post + the signature and format version embedded in the plan, never ambient config): rendered
  text/captions, entities, footers, slices, diagnostics codes/messages/
  details and album-overflow legitimacy all line up, so a structurally valid
  but forged snapshot cannot be persisted; recomputation lives ONLY here at
  save time — once frozen, the stored snapshot is exactly what gets sent, an
  in-flight or published stack is never re-planned on send;
- every read strictly re-parses the stored snapshot: invalid JSON, an
  unsupported plan version, a tampered signature URL or footer raise
  ContentValidationError — a corrupted row never reaches preview, schedule
  or the sender (dispatch.claim re-parses inside its transaction for the
  same reason);
- cancel_to_draft removes the publication row only: the plan survives with
  the post. Deleting the post cascades the plan row away (migrations V5).
"""
from __future__ import annotations

import sqlite3
import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass

from ..db import iso, utc_now
from ..domain.content import ALBUM_MAX, Content, MediaRef
from ..domain.errors import (
    ContentValidationError,
    PublicationNotFound,
    TransitionError,
)
from ..domain.planner import (
    ALBUM_OVERFLOW,
    LogicalPost,
    PublicationPlan,
    plan_post_for_version,
)
from ..domain.original_forward import (
    FrozenPublicationPlan, OriginalForwardInput, OriginalForwardPlan,
    PublicationInput, parse_publication_input, parse_publication_plan,
    parse_send_snapshot, plan_primary_content,
)
from .publications import immediate_tx

#: from these statuses the planned stack is immutable (claim already froze it)
FROZEN_STATUSES = frozenset({"sending", "uncertain", "published"})


def _source_entry_size(key: tuple, result: frozenset[tuple[int, int]]) -> int:
    # Conservative payload accounting (including coordinate objects), in
    # addition to the fixed entry-count bound on Python container overhead.
    return (sum(len(value) * 4 if isinstance(value, str) else 32 for value in key)
            + len(result) * 256)


def _epoch(now_epoch: int | None) -> int:
    return int(utc_now().timestamp()) if now_epoch is None else now_epoch


def planned_media(plan: PublicationPlan) -> tuple[MediaRef, ...]:
    """Every media item of the plan in physical send order (album items
    expanded in place, single media as itself, text parts contribute none)."""
    found: list[MediaRef] = []
    for part in plan.parts:
        content = part.content
        if content.kind == "album" and content.items:
            found.extend(content.items)
        elif content.media is not None:
            found.append(content.media)
    return tuple(found)


def validate_plan_pair(logical: PublicationInput, plan: FrozenPublicationPlan) -> None:
    """Reject a (logical, plan) pair that is not EXACTLY the deterministic
    planner's own output for that logical post and the signature embedded in
    the plan.

    Two layers, cheapest first, so owners get the targeted message:
    1. lossless body: concatenating the part slices must reproduce the owner's
       body character-for-character; the planned items must equal the logical
       items in (kind, file_id) ORDER with the planner's own album-overflow
       warning as the only accepted 10-item truncation;
    2. deterministic equality: ``plan_post_for_version`` is a pure function of
       (logical, signature, version), so recomputing it and comparing the canonical
       ``to_json()`` snapshots pins EVERYTHING the cheap guards cannot see —
       each part's rendered text/caption versus its slice, footer, entities,
       media items' own captions, and every diagnostic's code, message and
       details. A fabricated overflow claim (wrong details, or the code at
       all on <= ALBUM_MAX items), diverging rendered text, stripped entities
       or a dropped item caption are forgeries, not plans, and are refused
       before any write. The comparison uses the plan's OWN signature: an
        environment/config or current footer-format change cannot invalidate an already frozen pair,
       only re-planning can.

    This recompute runs at SAVE validation only. Publication sends the frozen
    snapshot as stored (dispatch.claim strictly re-parses it); an accepted
    plan is never re-derived on send."""
    if isinstance(logical, OriginalForwardInput) or isinstance(plan, OriginalForwardPlan):
        if type(logical) is not OriginalForwardInput or type(plan) is not OriginalForwardPlan:
            raise ContentValidationError("native input and native plan must be a type-matched pair")
        if logical.source != plan.source:
            raise ContentValidationError("native plan must preserve the complete accepted source and group")
        # Reparse at this durable trust boundary, including strict owner/source
        # binding, ordered IDs and grouping validation; no remote source check.
        OriginalForwardInput.from_json(logical.to_json())
        OriginalForwardPlan.from_json(plan.to_json())
        return
    if not isinstance(logical, LogicalPost):
        raise ContentValidationError("logical must be a LogicalPost")
    if not isinstance(plan, PublicationPlan):
        raise ContentValidationError("plan must be a PublicationPlan")
    if plan.reconstructed_body() != (logical.body or ""):
        raise ContentValidationError(
            "plan parts do not reconstruct the logical body losslessly")
    overflow = any(d.code == ALBUM_OVERFLOW for d in plan.diagnostics)
    expected = logical.media[:ALBUM_MAX] if overflow else logical.media
    stored = planned_media(plan)
    if [(m.kind, m.file_id) for m in stored] != \
            [(m.kind, m.file_id) for m in expected]:
        raise ContentValidationError(
            "plan parts must carry the logical media in arrival order")
    result = plan_post_for_version(logical, plan.signature, version=plan.version)
    if result.plan is None:
        codes = ", ".join(sorted({d.code for d in result.diagnostics}))
        raise ContentValidationError(
            "the deterministic planner rejects this logical post with the"
            " signature embedded in the plan"
            f" ({codes}); the snapshot cannot be accepted")
    if result.plan.to_json() != plan.to_json():
        raise ContentValidationError(
            "plan snapshot is not the deterministic planner output for this"
            " logical post and embedded signature")


@dataclass(frozen=True)
class StoredPlan:
    post_id: int
    logical: PublicationInput
    plan: FrozenPublicationPlan
    plan_version: int
    created_at: str
    updated_at: str


class PlanStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # Only successful pure validation results, never database query results
        # or mutable domain objects. Conservative UTF-8 upper bound on keys.
        self._source_cache: OrderedDict[tuple, frozenset[tuple[int, int]]] = OrderedDict()
        self._source_cache_bytes = 0

    def _cached_sources(self, key: tuple, parse) -> frozenset[tuple[int, int]]:
        if key in self._source_cache:
            self._source_cache.move_to_end(key)
            return self._source_cache[key]
        result = parse()  # exceptions are never cached as empty protection
        size = _source_entry_size(key, result)
        if size <= 4 * 1024 * 1024:
            while self._source_cache and (len(self._source_cache) >= 128 or
                    self._source_cache_bytes + size > 4 * 1024 * 1024):
                old, old_result = self._source_cache.popitem(last=False)
                self._source_cache_bytes -= _source_entry_size(old, old_result)
            self._source_cache[key] = result
            self._source_cache_bytes += size
        return result

    def content_snapshot(self, post_id: int) -> str:
        """Semantic draft-edit CAS: content AND complete frozen pair/absence.

        Independent of UI versions and timestamps. The writer calls this again
        under its immediate transaction; invalid stored pairs fail closed.
        """
        post = self._conn.execute('SELECT content_json FROM posts WHERE id=?',
                                  (post_id,)).fetchone()
        if post is None:
            raise PublicationNotFound(f'post {post_id} not found')
        stored = self.get(post_id)
        values = [post_id, Content.from_json(post['content_json']).to_json(),
                  stored.logical.to_json() if stored else None,
                  stored.plan.to_json() if stored else None]
        return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()

    # ---- writes ----------------------------------------------------------
    def save(
        self,
        post_id: int,
        logical: PublicationInput,
        plan: FrozenPublicationPlan,
        *,
        now_epoch: int | None = None,
    ) -> StoredPlan:
        """Atomically replace the frozen pair for this post (draft/scheduled/
        suspended/missed only). One row per post; created_at survives."""
        return self._write(post_id, logical, plan, None, now_epoch)

    def save_with_content(
        self,
        post_id: int,
        logical: PublicationInput,
        plan: FrozenPublicationPlan,
        content: Content,
        *,
        now_epoch: int | None = None,
        expected_snapshot: str | None = None,
    ) -> StoredPlan:
        """Save the pair AND re-sync posts.content_json to the plan's first
        physical part in one transaction — while the post is still a draft.
        Scheduled content changes keep flowing through the existing CAS
        PublicationsService.edit_content contract instead."""
        return self._write(post_id, logical, plan, content, now_epoch, expected_snapshot)

    def _write(
        self,
        post_id: int,
        logical: PublicationInput,
        plan: FrozenPublicationPlan,
        content: Content | None,
        now_epoch: int | None,
        expected_snapshot: str | None = None,
    ) -> StoredPlan:
        validate_plan_pair(logical, plan)
        logical_json = logical.to_json()      # canonical, validates first
        plan_json = plan.to_json()
        content_json: str | None = None
        if content is not None:
            first = plan_primary_content(plan).to_json()
            if content.to_json() != first:
                raise ContentValidationError(
                    "draft content must be the first physical part of the"
                    " plan (legacy readers of posts.content_json must see"
                    " exactly what part 1 sends)")
            content_json = first
        now = _epoch(now_epoch)
        stamp = iso(now)
        with immediate_tx(self._conn) as conn:
            if (expected_snapshot is not None and
                    self.content_snapshot(post_id) != expected_snapshot):
                raise TransitionError('draft content changed since review; review it again')
            post = conn.execute(
                "SELECT content_json FROM posts WHERE id=?", (post_id,)
            ).fetchone()
            if post is None:
                raise PublicationNotFound(f"post {post_id} not found")
            if isinstance(plan, OriginalForwardPlan) and content_json is None:
                if post["content_json"] != plan_primary_content(plan).to_json():
                    raise ContentValidationError(
                        "native plan requires the complete canonical forward content projection")
            status_row = conn.execute(
                "SELECT status FROM publications WHERE post_id=?", (post_id,)
            ).fetchone()
            status = status_row["status"] if status_row is not None else None
            if status in FROZEN_STATUSES:
                raise TransitionError(
                    f"the plan is frozen while the publication is {status};"
                    " an already planned or in-flight stack is immutable")
            if content_json is not None and status is not None:
                raise TransitionError(
                    "content re-sync is only allowed while the post is still"
                    " a draft; scheduled content changes go through the"
                    " compare-and-set content edit")
            if content_json is not None:
                cur = conn.execute(
                    "UPDATE posts SET content_json=?, updated_at=?"
                    " WHERE id=?",
                    (content_json, stamp, post_id),
                )
                assert cur.rowcount == 1
            conn.execute(
                "INSERT INTO post_plans(post_id, logical_json, plan_json,"
                " plan_version, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(post_id) DO UPDATE SET"
                " logical_json=excluded.logical_json,"
                " plan_json=excluded.plan_json,"
                " plan_version=excluded.plan_version,"
                " updated_at=excluded.updated_at",
                (post_id, logical_json, plan_json, plan.version, stamp, stamp),
            )
        stored = self.get(post_id)
        assert stored is not None
        return stored

    # ---- strict reads ------------------------------------------------------
    def get(self, post_id: int) -> StoredPlan | None:
        """Strictly re-parses the stored pair; a tampered or unsupported
        snapshot raises ContentValidationError instead of being served."""
        row = self._conn.execute(
            "SELECT * FROM post_plans WHERE post_id=?", (post_id,)
        ).fetchone()
        if row is None:
            return None
        return self._stored_row(row)

    def _stored_row(self, row, *, content_json: str | None = None) -> StoredPlan:
        logical = parse_publication_input(row["logical_json"])
        plan = parse_publication_plan(row["plan_json"])
        if isinstance(logical, OriginalForwardInput) or isinstance(plan, OriginalForwardPlan):
            validate_plan_pair(logical, plan)
            if content_json is None:
                post = self._conn.execute(
                    "SELECT content_json FROM posts WHERE id=?", (row['post_id'],),
                ).fetchone()
                content_json = post['content_json'] if post is not None else None
            if content_json != plan_primary_content(plan).to_json():
                raise ContentValidationError(
                    "native plan and stored forward content projection disagree")
        if row["plan_version"] != plan.version:
            raise ContentValidationError(
                "stored plan version does not match the planner")
        return StoredPlan(
            post_id=row["post_id"],
            logical=logical,
            plan=plan,
            plan_version=row["plan_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def has_plan(self, post_id: int) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM post_plans WHERE post_id=?", (post_id,)
        ).fetchone() is not None

    def protected_original_forward_coordinates(self) -> frozenset[tuple[int, int]]:
        """Read-only cleanup guard: every retained plan AND intent reference.

        Never infer safety from current posts.content_json: a post may now be
        a copy while an older native intent is still retained. Any unreadable
        record raises instead of returning an incomplete protection set. The
        caller must skip deletion on failure and coordinate concurrent writes;
        this query neither deletes messages nor authorizes their deletion.
        """
        protected: set[tuple[int, int]] = set()
        # Fresh complete rows on EVERY guard, including the last-millisecond
        # per-delete guard. Exact raw keys invalidate even external writes;
        # no version/timestamp cooperation is required from another writer.
        for row in self._conn.execute(
            'SELECT p.*, posts.content_json FROM post_plans p'
            ' LEFT JOIN posts ON posts.id=p.post_id'
        ).fetchall():
            key = ('plan-parser-1', row['logical_json'], row['plan_json'],
                   row['plan_version'], row['content_json'])

            def read_plan():
                stored = self._stored_row(row, content_json=row['content_json'])
                return (frozenset((stored.plan.source.chat_id, mid)
                                  for mid in stored.plan.source.message_ids)
                        if isinstance(stored.plan, OriginalForwardPlan) else frozenset())

            protected.update(self._cached_sources(key, read_plan))
        for row in self._conn.execute(
            "SELECT part_snapshot_json FROM publication_parts"
        ).fetchall():
            def read_intent():
                snapshot = parse_send_snapshot(row['part_snapshot_json'])
                return (frozenset((snapshot.source.chat_id, mid)
                                  for mid in snapshot.source.message_ids)
                        if isinstance(snapshot, OriginalForwardPlan) else frozenset())

            protected.update(self._cached_sources(
                ('intent-parser-1', row['part_snapshot_json']), read_intent))
        return frozenset(protected)
