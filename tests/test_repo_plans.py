"""CHUNK2 storage contract: durable versioned logical post + frozen plan.

Encoded decisions (approved brief):
- the frozen (logical, plan) pair lives in post_plans keyed by post_id —
  never overloaded into posts.content_json (single physical Content
  compatibility) or ui_state (volatile conversation state);
- a stored pair is strictly re-validated on every read: invalid JSON, an
  unsupported plan version, a tampered signature URL or a footer that no
  longer matches the published parts all raise ContentValidationError —
  the repository never serves a tampered snapshot to preview/schedule/send;
- saves replace atomically while the composition is editable (no
  publication, scheduled, suspended, missed) and are refused while the
  publication is sending, uncertain or published: an already planned or
  already in-flight stack is immutable (signature/settings changes in the
  environment can never mutate it);
- save refuses pairs that do not line up: the parts must reconstruct the
  logical body exactly and carry the logical media in order (album overflow
  may keep only the first ALBUM_MAX items — the planner's warning makes the
  truncation explicit, nothing else may diverge);
- save proves the plan DETERMINISTIC: the pure planner is recomputed from the
  pair itself (logical post + the signature EMBEDDED IN THE PLAN, never from
  the current environment) and its canonical snapshot must equal the stored
  one exactly — rendered text/captions, entities, footers, slices, diagnostics
  and album-overflow legitimacy included. A forged snapshot (rendered text
  diverging from its slices, dropped body entities, lost item captions, a
  fabricated or misreported album_overflow) cannot pass; recomputation happens
  ONLY at save — publication sends the frozen snapshot as stored and never
  re-plans on send;
- posts.content_json can only be re-synced to the plan atomically with the
  plan write and only while the post is still a draft (scheduled content
  changes keep flowing through the existing CAS edit_content contract).
"""
from __future__ import annotations

import json
import sqlite3
import asyncio

import pytest

from conftest import NOW, future, text_content
from poster_bot.domain.content import (
    ALBUM_MAX,
    Content,
    Entity,
    MediaRef,
    utf16_length,
)
from poster_bot.domain.errors import (
    ContentValidationError,
    PublicationNotFound,
    TransitionError,
)
from poster_bot.domain.planner import (
    ALBUM_OVERFLOW,
    PLAN_VERSION,
    LogicalPost,
    PlanDiagnostic,
    PlannedPart,
    PublicationPlan,
    plan_post,
)
from poster_bot.domain.signature import SIG_JOIN, SignatureConfig
from poster_bot.repo.plans import PlanStore, validate_plan_pair
from poster_bot.repo.receipts import PartReceiptStore
from test_domain_planner_signature import LEGACY_V2_JSON

SIG = SignatureConfig(
    main_text="@channel_main",
    main_url="https://t.me/channel_main",
    partner_text="@channel_partner",
    partner_url="https://t.me/channel_partner",
)
#: a DIFFERENT signature: proves revalidation uses the signature EMBEDDED IN
#: THE PLAN, never the ambient/test configuration
SIG2 = SignatureConfig(
    main_text="@frozen_main",
    main_url="https://t.me/frozen_main",
    partner_text="@frozen_partner",
    partner_url="https://t.me/frozen_partner",
)
NL2 = "\n\n"
SIG_TEXT = SIG.rendered()


def plan_for(logical: LogicalPost):
    result = plan_post(logical, SIG)
    assert result.plan is not None, result.diagnostics
    return result.plan


def _sig_entities(full: str) -> tuple[Entity, Entity]:
    """Signature link entities placed over the signature at the END of
    ``full`` — exactly what a structurally valid (but possibly forged) part
    must carry to satisfy PublicationPlan's own construction guards."""
    return SIG.link_entities(utf16_length(full) - utf16_length(SIG_TEXT))


def _forged_single_text(text: str, entities, body_slice: str) -> PublicationPlan:
    """Single text part carrying arbitrary published text/entities above the
    mandatory signature line — passes every structural guard by construction."""
    return PublicationPlan(
        parts=(PlannedPart(index=1,
                           content=Content(kind="text", text=text,
                                           entities=tuple(entities)),
                           body_slice=body_slice),),
        signature=SIG,
    )


def three_part() -> tuple[LogicalPost, object]:
    logical = LogicalPost(body="A" * 3000 + NL2 + "B" * 3000 + NL2 + "C" * 3000)
    plan = plan_for(logical)
    assert len(plan.parts) == 3
    return logical, plan


def _photo(i: int, caption: str | None = None) -> MediaRef:
    return MediaRef(kind="photo", file_id=f"ph{i}", caption=caption)


def _plan_store(services) -> PlanStore:
    return PlanStore(services.conn)


def _row(conn, post_id: int):
    return conn.execute(
        "SELECT * FROM post_plans WHERE post_id=?", (post_id,)
    ).fetchone()


def _frozen_pair(version: int) -> tuple[LogicalPost, PublicationPlan]:
    logical = LogicalPost(media=(MediaRef(kind="photo", file_id="legacy",
                                          caption="x" * 1020),))
    legacy = PublicationPlan.from_json(LEGACY_V2_JSON)
    if version == 2:
        return logical, legacy
    result = plan_post(logical, legacy.signature)
    assert result.plan is not None, result.diagnostics
    assert result.plan.version == version
    return logical, result.plan


class TestSaveRead:
    @pytest.mark.parametrize("version", [2, 3])
    def test_create_with_plan_preserves_supplied_frozen_version(self, services, version):
        logical, plan = _frozen_pair(version)
        expected_json = LEGACY_V2_JSON if version == 2 else plan.to_json()

        post_id = services.drafts.create_with_plan(logical, plan, now_epoch=NOW)

        loaded = _plan_store(services).get(post_id)
        assert loaded is not None
        assert loaded.plan.version == version
        assert loaded.plan.to_json() == expected_json
        assert loaded.logical.to_json() == logical.to_json()
        assert _row(services.conn, post_id)["plan_version"] == version
        assert _row(services.conn, post_id)["plan_json"] == expected_json
        assert services.drafts.get_content(post_id) == plan.parts[0].content
        assert [p.published_text() for p in loaded.plan.parts] == [
            p.published_text() for p in plan.parts]

    def test_legacy_v2_save_get_claim_and_send_preserve_frozen_bytes(self, services):
        from fake_transport import FakeTransport
        from planned_helpers import gated_sender

        logical = LogicalPost(media=(MediaRef(kind="photo", file_id="legacy",
                                              caption="x" * 1020),))
        legacy = PublicationPlan.from_json(LEGACY_V2_JSON)
        plans = _plan_store(services)
        post_id = services.drafts.create(legacy.parts[0].content, now_epoch=NOW)
        plans.save(post_id, logical, legacy, now_epoch=NOW)
        assert _row(services.conn, post_id)["plan_version"] == 2
        assert plans.get(post_id).plan.to_json() == LEGACY_V2_JSON
        assert plans.get(post_id).logical.to_json() == logical.to_json()
        pub = services.publications.schedule(post_id, future(), now_epoch=NOW)
        transport = FakeTransport()
        results = asyncio.run(gated_sender(services, transport).send_due(
            now_epoch=pub.scheduled_epoch))
        assert [r.outcome for r in results] == ["published"]
        assert [c.to_json() for c in transport.calls] == [
            p.content.to_json() for p in legacy.parts]
        assert [r.text for r in transport.edit_calls] == [
            p.published_text() for p in legacy.parts]
        attempt = services.conn.execute(
            "SELECT id FROM send_attempts WHERE publication_id=?", (pub.id,)
        ).fetchone()["id"]
        snapshots = PartReceiptStore(services.conn).parts_of_attempt(attempt)
        assert [p.snapshot for p in snapshots] == list(legacy.parts)
        assert plans.get(post_id).plan.to_json() == LEGACY_V2_JSON
        for part, request in zip(legacy.parts, transport.edit_calls):
            published = part.published_text()
            assert published.endswith("Часть 1 | Часть 2\n@m | @p")
            entities = (part.content.entities if part.content.kind == "text"
                        else part.content.media.caption_entities)
            assert entities[-2:] == legacy.signature.link_entities(
                utf16_length(published) - 7)
            assert tuple(e for e in request.entities if e.url in (
                legacy.signature.main_url, legacy.signature.partner_url)) == entities[-2:]

    @pytest.mark.parametrize("version", [2, 3])
    def test_save_revalidates_with_exact_frozen_version_and_rejects_forgeries(self, services, version):
        from dataclasses import replace
        from poster_bot.domain.planner import plan_post_for_version

        logical = LogicalPost(body="A" * (4096 - 33))
        # This exact single paragraph fits v2 but not v3: validating v2 with
        # the new planner would incorrectly condemn a legitimate frozen pair.
        if version == 3:
            logical = LogicalPost(body="Body\n\n\n")
        else:
            assert plan_post(logical, SIG).plan is None
        plan = plan_post_for_version(logical, SIG, version=version).plan
        assert plan is not None
        validate_plan_pair(logical, plan)
        post_id = services.drafts.create(plan.parts[0].content, now_epoch=NOW)
        plans = _plan_store(services)
        plans.save(post_id, logical, plan, now_epoch=NOW)
        assert plans.get(post_id).plan.version == version
        part = plan.parts[0]
        forged_text = part.content.text.replace("A" if version == 2 else "Body", "Z", 1)
        # Use a length-preserving replacement so structural validation passes.
        if version == 3:
            forged_text = part.content.text.replace("Body", "Evil", 1)
        forged = replace(plan, parts=(replace(part, content=Content(
            kind="text", text=forged_text, entities=part.content.entities)),))
        with pytest.raises(ContentValidationError, match="deterministic"):
            plans.save(post_id, logical, forged, now_epoch=NOW)
        assert plans.get(post_id).plan.to_json() == plan.to_json()

    def test_roundtrip_fidelity(self, services):
        plans = _plan_store(services)
        logical, plan = three_part()
        post_id = services.drafts.create(plan.parts[0].content,
                                         now_epoch=NOW)
        saved = plans.save(post_id, logical, plan, now_epoch=NOW)
        loaded = plans.get(post_id)
        assert loaded is not None
        assert loaded.plan.to_json() == plan.to_json()
        assert loaded.logical.to_json() == logical.to_json()
        assert saved.plan.signature == loaded.plan.signature
        assert loaded.plan.reconstructed_body() == logical.body
        assert [p.content.to_json() for p in loaded.plan.parts] == \
            [p.content.to_json() for p in plan.parts]

    def test_plan_version_is_stored(self, services):
        plans = _plan_store(services)
        logical, plan = three_part()
        post_id = services.drafts.create(plan.parts[0].content,
                                         now_epoch=NOW)
        plans.save(post_id, logical, plan, now_epoch=NOW)
        row = _row(services.conn, post_id)
        assert row["plan_version"] == PLAN_VERSION

    def test_get_missing_returns_none(self, services):
        plans = _plan_store(services)
        post_id = services.drafts.create(text_content(), now_epoch=NOW)
        assert plans.get(post_id) is None
        assert plans.has_plan(post_id) is False

    def test_media_order_captions_survive(self, services):
        plans = _plan_store(services)
        logical = LogicalPost(media=(_photo(1), MediaRef(kind="video",
                                                         file_id="vd2")))
        plan = plan_for(logical)
        post_id = services.drafts.create(plan.parts[0].content,
                                         now_epoch=NOW)
        plans.save(post_id, logical, plan, now_epoch=NOW)
        loaded = plans.get(post_id)
        stored = [m for part in loaded.plan.parts
                  for m in (list(part.content.items)
                            if part.content.items
                            else ([part.content.media]
                                  if part.content.media else []))]
        assert [(m.kind, m.file_id, m.caption) for m in stored] == \
            [(m.kind, m.file_id, m.caption) for m in logical.media]

    def test_post_must_exist(self, services):
        plans = _plan_store(services)
        logical, plan = three_part()
        with pytest.raises(PublicationNotFound):
            plans.save(9999, logical, plan, now_epoch=NOW)
        assert services.conn.execute(
            "SELECT COUNT(*) c FROM post_plans").fetchone()["c"] == 0

    def test_raw_insert_for_unknown_post_rejected(self, services):
        with pytest.raises(sqlite3.IntegrityError):
            services.conn.execute(
                "INSERT INTO post_plans(post_id, logical_json, plan_json,"
                " plan_version, created_at, updated_at) VALUES (9999,"
                " '{}', '{}', 2, 'x', 'x')")

    def test_cross_post_reads_are_isolated(self, services):
        plans = _plan_store(services)
        logical, plan = three_part()
        post_a = services.drafts.create(plan.parts[0].content, now_epoch=NOW)
        post_b = services.drafts.create(text_content("other"), now_epoch=NOW)
        plans.save(post_a, logical, plan, now_epoch=NOW)
        assert plans.has_plan(post_b) is False
        assert plans.get(post_b) is None
        assert plans.has_plan(post_a) is True


class TestAtomicReplace:
    def test_replace_keeps_one_row_and_created_at(self, services):
        plans = _plan_store(services)
        logical, plan = three_part()
        post_id = services.drafts.create(plan.parts[0].content, now_epoch=NOW)
        plans.save(post_id, logical, plan, now_epoch=NOW)
        created = _row(services.conn, post_id)["created_at"]
        logical2 = LogicalPost(body="X" * 3000 + NL2 + "Y" * 3000)
        plan2 = plan_for(logical2)
        plans.save(post_id, logical2, plan2, now_epoch=NOW + 60)
        rows = services.conn.execute(
            "SELECT COUNT(*) c FROM post_plans").fetchall()
        assert rows[0]["c"] == 1
        row = _row(services.conn, post_id)
        assert row["created_at"] == created
        assert row["updated_at"] != created
        assert plans.get(post_id).plan.to_json() == plan2.to_json()

    def test_lossless_guard_rejects_mismatched_pair(self, services):
        plans = _plan_store(services)
        logical, plan = three_part()
        other = LogicalPost(body="completely different body")
        post_id = services.drafts.create(plan.parts[0].content, now_epoch=NOW)
        with pytest.raises(ContentValidationError):
            plans.save(post_id, other, plan, now_epoch=NOW)
        assert plans.get(post_id) is None

    def test_media_mismatch_rejected(self, services):
        plans = _plan_store(services)
        logical = LogicalPost(media=(_photo(1), _photo(2)))
        plan = plan_for(logical)
        swapped = LogicalPost(media=(_photo(1), _photo(3)))
        post_id = services.drafts.create(plan.parts[0].content, now_epoch=NOW)
        with pytest.raises(ContentValidationError):
            plans.save(post_id, swapped, plan, now_epoch=NOW)

    def test_album_overflow_prefix_is_allowed(self, services):
        plans = _plan_store(services)
        items = tuple(_photo(i) for i in range(1, 12))
        logical = LogicalPost(media=items)
        plan = plan_for(logical)  # warning diagnostic, 10 kept
        assert any(d.code == "album_overflow" for d in plan.diagnostics)
        post_id = services.drafts.create(plan.parts[0].content, now_epoch=NOW)
        saved = plans.save(post_id, logical, plan, now_epoch=NOW)
        assert saved.plan.to_json() == plan.to_json()

    def test_replace_allowed_while_scheduled(self, services):
        plans = _plan_store(services)
        logical, plan = three_part()
        post_id = services.drafts.create(plan.parts[0].content, now_epoch=NOW)
        plans.save(post_id, logical, plan, now_epoch=NOW)
        services.publications.schedule(post_id, future(), now_epoch=NOW)
        logical2 = LogicalPost(body="Z" * 3000 + NL2 + "W" * 3000)
        plan2 = plan_for(logical2)
        plans.save(post_id, logical2, plan2, now_epoch=NOW + 10)
        assert plans.get(post_id).plan.to_json() == plan2.to_json()


class TestDeterministicSnapshotEquality:
    """CHUNK2 correction: a saved pair must equal the DETERMINISTIC planner
    output for that exact logical post and the signature embedded in the plan
    itself. Lossless slices + media (kind, file_id) alone are NOT the pair's
    identity: rendered text, entities, media captions, footers, diagnostics
    and album-overflow legitimacy are compared through canonical snapshots.
    Recomputation happens ONLY here at save; publication sends the frozen
    snapshot as stored (dispatch.claim strictly re-parses, never re-plans)."""

    # ---- positive proof: determinism and embedded-signature revalidation ----
    def test_plan_post_is_deterministic(self):
        logical = LogicalPost(body="A" * 500 + NL2 + "B" * 500)
        first = plan_for(logical)
        assert plan_for(logical).to_json() == first.to_json()
        # canonical snapshot survives a strict from_json/to_json round trip:
        # fakes re-constructed via from_json are compared on the same footing
        assert PublicationPlan.from_json(first.to_json()).to_json() \
            == first.to_json()

    def test_legit_pair_revalidates_with_embedded_signature_only(self, services):
        plans = _plan_store(services)
        logical = LogicalPost(body="Embed " + "x" * 200)
        result = plan_post(logical, SIG2)  # NOT the ambient SIG of this module
        assert result.plan is not None, result.diagnostics
        validate_plan_pair(logical, result.plan)
        post_id = services.drafts.create(result.plan.parts[0].content,
                                         now_epoch=NOW)
        saved = plans.save(post_id, logical, result.plan, now_epoch=NOW)
        assert saved.plan.to_json() == result.plan.to_json()

    # ---- red: forgeries that pass every structural + old lossless guard ----
    def test_rendered_text_diverging_from_slice_rejected(self, services):
        plans = _plan_store(services)
        logical = LogicalPost(body="Hello")
        legit = plan_for(logical)
        forged_text = "Goodbye" + SIG_JOIN + SIG_TEXT
        forged = _forged_single_text(forged_text, _sig_entities(forged_text),
                                     "Hello")
        # the old guards accepted this: slice still reconstructs the body...
        assert forged.reconstructed_body() == logical.body
        # ...and the part is a fully valid Content/footer-free single part
        # whose text ends with the mandatory signature + link entities.
        validate_plan_pair(logical, legit)  # sanity: the real pair passes
        with pytest.raises(ContentValidationError):
            validate_plan_pair(logical, forged)
        post_id = services.drafts.create(legit.parts[0].content, now_epoch=NOW)
        with pytest.raises(ContentValidationError):
            plans.save(post_id, logical, forged, now_epoch=NOW)
        assert plans.get(post_id) is None  # nothing written

    def test_body_entity_tamper_rejected(self, services):
        logical = LogicalPost(
            body="Important note",
            entities=(Entity(type="bold", offset=0, length=9),))
        legit = plan_for(logical)
        published = legit.parts[0].published_text()
        # strip the owner's bold entity, keep text, slice and both signature
        # links: structurally still a valid plan, semantically a tamper
        stripped = tuple(legit.parts[0].content.entities[1:])
        assert len(stripped) == 2
        forged = _forged_single_text(published, stripped,
                                     legit.parts[0].body_slice)
        with pytest.raises(ContentValidationError):
            validate_plan_pair(logical, forged)

    def test_own_caption_loss_rejected(self, services):
        item = MediaRef(kind="photo", file_id="ph1",
                        caption="Важная подпись товара",
                        caption_entities=(Entity(type="bold", offset=0,
                                                 length=6),))
        logical = LogicalPost(media=(item,))
        legit = plan_for(logical)
        assert "Важная подпись товара" in legit.parts[0].published_text()
        # media identity (kind, file_id) kept, the item's OWN caption dropped:
        # the old media guard compared neither captions nor rendered text
        bare = MediaRef(kind="photo", file_id="ph1", caption=SIG_TEXT,
                        caption_entities=_sig_entities(SIG_TEXT))
        forged = PublicationPlan(
            parts=(PlannedPart(index=1,
                               content=Content(kind="photo", media=bare),
                               body_slice=""),),
            signature=SIG)
        assert forged.reconstructed_body() == (logical.body or "")
        with pytest.raises(ContentValidationError):
            validate_plan_pair(logical, forged)

    def test_fabricated_overflow_claim_on_small_album_rejected(self, services):
        items = tuple(_photo(i) for i in range(1, 9))  # 8: overflow impossible
        logical = LogicalPost(media=items)
        legit = plan_for(logical)
        assert not any(d.code == ALBUM_OVERFLOW for d in legit.diagnostics)
        fake = PlanDiagnostic(
            code=ALBUM_OVERFLOW,
            message="В альбоме 9 элементов, в план вошли первые 8",
            details={"received": 9, "kept": 8, "extra": 1})
        forged = PublicationPlan(parts=legit.parts, signature=SIG,
                                 diagnostics=(fake,))
        # the old guard truncated its expectation to ALBUM_MAX on the bare
        # claim of the code string — a free pass to fabricate truncation
        with pytest.raises(ContentValidationError):
            validate_plan_pair(logical, forged)

    def test_misreported_overflow_extra_rejected(self, services):
        items = tuple(_photo(i) for i in range(1, 16))  # 15: five never ship
        logical = LogicalPost(media=items)
        legit = plan_for(logical)
        honest = next(d for d in legit.diagnostics
                      if d.code == ALBUM_OVERFLOW)
        assert honest.details == {"received": 15, "kept": ALBUM_MAX,
                                  "extra": 5}
        # forge the warning to hide that THREE MORE valid items stay
        # unpublished than the owner is told: media (kind, file_id) matches
        # the first ALBUM_MAX exactly, so the old guard accepted the pair
        fake = PlanDiagnostic(
            code=ALBUM_OVERFLOW, message=honest.message,
            details={"received": 12, "kept": ALBUM_MAX, "extra": 2})
        forged = PublicationPlan(
            parts=legit.parts, signature=SIG,
            diagnostics=(fake,) + tuple(d for d in legit.diagnostics
                                        if d.code != ALBUM_OVERFLOW))
        with pytest.raises(ContentValidationError):
            validate_plan_pair(logical, forged)


class TestFrozenGuards:
    def _planned_scheduled(self, services, plans):
        logical, plan = three_part()
        post_id = services.drafts.create(plan.parts[0].content, now_epoch=NOW)
        plans.save(post_id, logical, plan, now_epoch=NOW)
        pub = services.publications.schedule(post_id, future(), now_epoch=NOW)
        return post_id, plan, pub

    def test_frozen_while_sending(self, services):
        plans = _plan_store(services)
        post_id, plan, pub = self._planned_scheduled(services, plans)
        services.dispatch.claim(pub.id, pub.version, now_epoch=NOW + 7200)
        before = _row(services.conn, post_id)["plan_json"]
        with pytest.raises(TransitionError):
            plans.save(post_id, LogicalPost(body="late idea"),
                       plan_for(LogicalPost(body="late idea")),
                       now_epoch=NOW + 7300)
        assert _row(services.conn, post_id)["plan_json"] == before

    def test_frozen_while_uncertain(self, services):
        plans = _plan_store(services)
        post_id, plan, pub = self._planned_scheduled(services, plans)
        attempt = services.dispatch.claim(pub.id, pub.version,
                                          now_epoch=NOW + 7200)
        services.dispatch.mark_uncertain(pub.id, attempt, "timeout",
                                         now_epoch=NOW + 7230)
        with pytest.raises(TransitionError):
            plans.save(post_id, *three_part(), now_epoch=NOW + 7400)

    def test_frozen_while_published(self, services):
        plans = _plan_store(services)
        post_id, plan, pub = self._planned_scheduled(services, plans)
        attempt = services.dispatch.claim(pub.id, pub.version,
                                          now_epoch=NOW + 7200)
        parts = services.conn.execute(
            "SELECT part_index FROM publication_parts WHERE attempt_id=?"
            " ORDER BY part_index", (attempt,)).fetchall()
        receipts = PartReceiptStore(services.conn)
        for i, part in enumerate(parts):
            receipts.record_confirmed(
                attempt, part["part_index"], (9000 + i,),
                f"https://t.me/c/777/{9000 + i}", now_epoch=NOW + 7240)
        services.dispatch.complete(
            pub.id, attempt,
            tuple(9000 + i for i in range(len(parts))),
            "https://t.me/c/777/9000", now_epoch=NOW + 7250)
        with pytest.raises(TransitionError):
            plans.save(post_id, *three_part(), now_epoch=NOW + 7500)

    def test_editable_while_suspended(self, services):
        plans = _plan_store(services)
        post_id, plan, pub = self._planned_scheduled(services, plans)
        services.publications.pause(pub.id, pub.version, now_epoch=NOW + 10)
        logical2 = LogicalPost(body="Q" * 3000 + NL2 + "R" * 3000)
        plan2 = plan_for(logical2)
        plans.save(post_id, logical2, plan2, now_epoch=NOW + 20)
        assert plans.get(post_id).plan.to_json() == plan2.to_json()

    def test_cancel_to_draft_keeps_the_plan(self, services):
        plans = _plan_store(services)
        post_id, plan, pub = self._planned_scheduled(services, plans)
        services.publications.cancel_to_draft(pub.id, pub.version,
                                              now_epoch=NOW + 10)
        assert plans.get(post_id).plan.to_json() == plan.to_json()

    def test_post_delete_cascades_the_plan(self, services):
        plans = _plan_store(services)
        post_id, plan, pub = self._planned_scheduled(services, plans)
        services.publications.delete_by_post(post_id, now_epoch=NOW + 10)
        assert _row(services.conn, post_id) is None

    def test_delete_via_publication_cascades_the_plan(self, services):
        plans = _plan_store(services)
        post_id, plan, pub = self._planned_scheduled(services, plans)
        services.publications.delete_post(pub.id, pub.version,
                                          now_epoch=NOW + 10)
        assert _row(services.conn, post_id) is None


class TestTamperRejection:
    """Reads re-validate strictly: a tampered snapshot never reaches
    preview, scheduling or the sender."""

    def _stored(self, services):
        plans = _plan_store(services)
        logical, plan = three_part()
        post_id = services.drafts.create(plan.parts[0].content, now_epoch=NOW)
        plans.save(post_id, logical, plan, now_epoch=NOW)
        return plans, post_id

    def _tamper(self, services, post_id, *, column, raw):
        services.conn.execute(
            f"UPDATE post_plans SET {column}=?, updated_at=? WHERE post_id=?",
            (raw, "2026-10-22T00:00:00+00:00", post_id))

    def test_invalid_json_rejected(self, services):
        plans, post_id = self._stored(services)
        self._tamper(services, post_id, column="plan_json", raw="{ not json")
        with pytest.raises(ContentValidationError):
            plans.get(post_id)

    def test_unsupported_version_rejected(self, services):
        plans, post_id = self._stored(services)
        data = json.loads(_row(services.conn, post_id)["plan_json"])
        data["v"] = PLAN_VERSION + 1
        self._tamper(services, post_id, column="plan_json",
                     raw=json.dumps(data))
        with pytest.raises(ContentValidationError):
            plans.get(post_id)

    def test_signature_url_tamper_rejected(self, services):
        plans, post_id = self._stored(services)
        data = json.loads(_row(services.conn, post_id)["plan_json"])
        data["signature"]["main_url"] = "ftp://evil.example/x"
        self._tamper(services, post_id, column="plan_json",
                     raw=json.dumps(data))
        with pytest.raises(ContentValidationError):
            plans.get(post_id)

    def test_footer_tamper_rejected(self, services):
        plans, post_id = self._stored(services)
        data = json.loads(_row(services.conn, post_id)["plan_json"])
        data["parts"][0]["footer"]["text"] = "Часть 1 | Часть 2"
        self._tamper(services, post_id, column="plan_json",
                     raw=json.dumps(data))
        with pytest.raises(ContentValidationError):
            plans.get(post_id)

    def test_logical_json_tamper_rejected(self, services):
        plans, post_id = self._stored(services)
        self._tamper(services, post_id, column="logical_json",
                     raw=json.dumps({"v": PLAN_VERSION}))
        with pytest.raises(ContentValidationError):
            plans.get(post_id)


class TestContentSync:
    """posts.content_json stays the FIRST physical part for legacy readers —
    but only through atomic, guarded writes."""

    @pytest.mark.parametrize("version", [2, 3])
    def test_edit_content_preserves_supplied_frozen_version(self, services, version):
        plans = _plan_store(services)
        logical, legacy = _frozen_pair(2)
        post_id = services.drafts.create(legacy.parts[0].content, now_epoch=NOW)
        plans.save(post_id, logical, legacy, now_epoch=NOW)
        pub = services.publications.schedule(post_id, future(), now_epoch=NOW)
        logical, replacement = _frozen_pair(version)
        expected_json = LEGACY_V2_JSON if version == 2 else replacement.to_json()

        edited = services.publications.edit_content(
            pub.id, pub.version, replacement.parts[0].content,
            logical=logical, plan=replacement, now_epoch=NOW + 10)

        loaded = plans.get(post_id)
        assert loaded is not None
        assert loaded.plan.version == version
        assert loaded.plan.to_json() == expected_json
        assert loaded.logical.to_json() == logical.to_json()
        assert _row(services.conn, post_id)["plan_version"] == version
        assert _row(services.conn, post_id)["plan_json"] == expected_json
        assert services.drafts.get_content(post_id) == replacement.parts[0].content
        assert [p.published_text() for p in loaded.plan.parts] == [
            p.published_text() for p in replacement.parts]
        assert edited.status.value == "suspended"
        assert edited.version == pub.version + 1

    def test_save_with_content_updates_both_atomically(self, services):
        plans = _plan_store(services)
        logical, plan = three_part()
        post_id = services.drafts.create(text_content("stale"), now_epoch=NOW)
        plans.save_with_content(post_id, logical, plan,
                                plan.parts[0].content, now_epoch=NOW + 5)
        assert services.drafts.get_content(post_id) == plan.parts[0].content
        assert plans.get(post_id).plan.to_json() == plan.to_json()

    def test_save_with_content_refused_once_scheduled(self, services):
        plans = _plan_store(services)
        logical, plan = three_part()
        post_id = services.drafts.create(plan.parts[0].content, now_epoch=NOW)
        plans.save(post_id, logical, plan, now_epoch=NOW)
        services.publications.schedule(post_id, future(), now_epoch=NOW)
        with pytest.raises(TransitionError):
            plans.save_with_content(post_id, logical, plan,
                                    plan.parts[0].content, now_epoch=NOW + 5)
        assert plans.get(post_id).plan.to_json() == plan.to_json()

    def test_edit_content_with_plan_replaces_both(self, services):
        plans = _plan_store(services)
        logical, plan = three_part()
        post_id = services.drafts.create(plan.parts[0].content, now_epoch=NOW)
        plans.save(post_id, logical, plan, now_epoch=NOW)
        pub = services.publications.schedule(post_id, future(), now_epoch=NOW)
        logical2 = LogicalPost(body="N" * 3000 + NL2 + "M" * 3000)
        plan2 = plan_for(logical2)
        services.publications.edit_content(pub.id, pub.version,
                                           plan2.parts[0].content,
                                           logical=logical2, plan=plan2,
                                           now_epoch=NOW + 10)
        content = services.dispatch.content_for_publication(pub.id)
        assert content == plan2.parts[0].content
        assert plans.get(post_id).plan.to_json() == plan2.to_json()
        assert services.publications.get(pub.id).status.value == "suspended"

    def test_edit_content_plan_half_pair_refused(self, services):
        plans = _plan_store(services)
        logical, plan = three_part()
        post_id = services.drafts.create(plan.parts[0].content, now_epoch=NOW)
        plans.save(post_id, logical, plan, now_epoch=NOW)
        pub = services.publications.schedule(post_id, future(), now_epoch=NOW)
        with pytest.raises(TransitionError):
            services.publications.edit_content(pub.id, pub.version,
                                               plan.parts[0].content,
                                               logical=logical,
                                               now_epoch=NOW + 10)

    def test_edit_content_mismatched_plan_leaves_nothing_changed(
            self, services):
        plans = _plan_store(services)
        logical, plan = three_part()
        post_id = services.drafts.create(plan.parts[0].content, now_epoch=NOW)
        plans.save(post_id, logical, plan, now_epoch=NOW)
        pub = services.publications.schedule(post_id, future(), now_epoch=NOW)
        stranger = LogicalPost(body="stranger body")
        before_content = services.conn.execute(
            "SELECT content_json FROM posts WHERE id=?",
            (post_id,)).fetchone()["content_json"]
        before_plan = _row(services.conn, post_id)["plan_json"]
        with pytest.raises(ContentValidationError):
            services.publications.edit_content(pub.id, pub.version,
                                               plan.parts[0].content,
                                               logical=stranger,
                                               plan=plan_for(stranger),
                                               now_epoch=NOW + 10)
        assert services.conn.execute(
            "SELECT content_json FROM posts WHERE id=?",
            (post_id,)).fetchone()["content_json"] == before_content
        assert _row(services.conn, post_id)["plan_json"] == before_plan
        assert services.publications.get(pub.id).status.value == "scheduled"


class TestSchemaChecks:
    def test_object_shape_checks(self, services):
        with pytest.raises(sqlite3.IntegrityError):
            services.conn.execute(
                "INSERT INTO post_plans(post_id, logical_json, plan_json,"
                " plan_version, created_at, updated_at) VALUES (1,"
                " '[1,2]', '{}', 2, 'x', 'x')")
        with pytest.raises(sqlite3.IntegrityError):
            services.conn.execute(
                "INSERT INTO post_plans(post_id, logical_json, plan_json,"
                " plan_version, created_at, updated_at) VALUES (1,"
                " '{}', '{}', 0, 'x', 'x')")
