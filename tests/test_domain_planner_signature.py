"""CHUNK1b planner acceptance: the mandatory signature footer.

Encoded decisions (approved brief):
- every published text part, single-media caption and album caption carries
  the signature as its BOTTOM line — no optional flag, no bypass path;
- footer order: body, blank line, navigation (multi-part only), blank line,
  signature; single-part plans get the signature but no navigation;
- budgets (4096/1024) include the signature; the exact-N fixed point is
  recomputed with the signature overhead on both media and text parts;
- a paragraph that fits the bare Telegram limit but not the signature budget
  asks for a manual split — the body is never clipped or truncated;
- the mandatory footer alone never silently clips: config caps (labels <= 64,
  URLs <= 512) keep it feasible; over-budget cases come back as diagnostics;
- signature links are 'text_link' entities over the displayed labels
  (UTF-16 offsets), merged after the body entities; never raw HTML, never
  clickable callbacks from untrusted input;
- media-only single (CHUNK1b correction): an own caption that cannot share
  the caption budget with the signature moves WHOLE into a following text
  part below a signature-only media part — never rejected while a text part
  can hold it, never auto-split, never clipped;
- the album caption + per-item-caption rejection is the DOMAIN's own Content
  rule (the Bot API payload allows the combination); owner-facing wording
  states this accurately and never claims Telegram rejects it;
- the frozen snapshot embeds the configured signature (labels AND URLs), so
  a later .env change cannot silently alter an already previewed/scheduled
  plan; format v3 adds the blank paragraph, while frozen v2 remains readable.
"""
from __future__ import annotations

import json

import pytest

from poster_bot.domain.content import (
    CAPTION_MAX,
    Entity,
    MediaRef,
    TEXT_MAX,
    utf16_length,
)
from poster_bot.domain.errors import ContentValidationError
from poster_bot.domain.planner import (
    FOOTER_JOIN,
    MANUAL_SPLIT_REQUIRED,
    PLAN_VERSION,
    SIGNATURE_CONFLICT,
    LogicalPost,
    PublicationPlan,
    footer_text,
    plan_post,
    total_footer_length,
)
from poster_bot.domain.signature import SIG_JOIN, SignatureConfig
from poster_bot.config import load_config

SIG = SignatureConfig(
    main_text="@channel_main",
    main_url="https://t.me/channel_main",
    partner_text="@channel_partner",
    partner_url="https://t.me/channel_partner",
)
SIG_TEXT = "@channel_main | @channel_partner"  # 32 rendered chars
NL2 = "\n\n"

# Captured from the v2 planner before the separator change. Only the repeated
# caption is abbreviated; all serialized fields/offsets are frozen literals.
LEGACY_V2_JSON = r'''{"diagnostics": [], "parts": [{"body_slice": "", "content_json": "{\"kind\": \"photo\", \"media\": {\"caption\": \"Часть 1 | Часть 2\\n@m | @p\", \"caption_entities\": [{\"length\": 2, \"offset\": 18, \"type\": \"text_link\", \"url\": \"https://t.me/m\"}, {\"length\": 2, \"offset\": 23, \"type\": \"text_link\", \"url\": \"https://t.me/p\"}], \"file_id\": \"legacy\", \"kind\": \"photo\"}, \"mode\": \"copy\"}", "footer": {"slots": [{"label": "Часть 1", "linked": false, "part_index": 1, "utf16_length": 7, "utf16_offset": 0}, {"label": "Часть 2", "linked": true, "part_index": 2, "utf16_length": 7, "utf16_offset": 10}], "text": "Часть 1 | Часть 2"}, "index": 1}, {"body_slice": "", "content_json": "{\"entities\": [{\"length\": 2, \"offset\": 1040, \"type\": \"text_link\", \"url\": \"https://t.me/m\"}, {\"length\": 2, \"offset\": 1045, \"type\": \"text_link\", \"url\": \"https://t.me/p\"}], \"kind\": \"text\", \"mode\": \"copy\", \"text\": \"CAPTION_PLACEHOLDER\\n\\nЧасть 1 | Часть 2\\n@m | @p\"}", "footer": {"slots": [{"label": "Часть 1", "linked": true, "part_index": 1, "utf16_length": 7, "utf16_offset": 1022}, {"label": "Часть 2", "linked": false, "part_index": 2, "utf16_length": 7, "utf16_offset": 1032}], "text": "Часть 1 | Часть 2"}, "index": 2}], "signature": {"main_text": "@m", "main_url": "https://t.me/m", "partner_text": "@p", "partner_url": "https://t.me/p"}, "v": 2}'''.replace("CAPTION_PLACEHOLDER", "x" * 1020)


@pytest.mark.parametrize("media", [(), (MediaRef(kind="photo", file_id="a"),),
                                   (MediaRef(kind="photo", file_id="a"),
                                    MediaRef(kind="photo", file_id="b"))])
def test_one_blank_paragraph_before_signature_single_and_multipart(media):
    for body in ("текст\n\n\n \n", "A" * 3000 + "\n\n\n" + "B" * 3000 + "\n\n\n"):
        plan = plan_post(LogicalPost(body=body, media=media), SIG).plan
        assert plan is not None
        assert plan.reconstructed_body() == body
        for part in plan.parts:
            published = part.published_text()
            assert published.endswith("\n\n" + SIG_TEXT)
            assert not published.endswith("\n\n\n" + SIG_TEXT)
            if part.footer:
                assert published.endswith(part.footer.text + "\n\n" + SIG_TEXT)
            else:
                assert published == "текст\n\n" + SIG_TEXT


def test_captured_v2_snapshot_roundtrip_is_byte_exact():
    assert PublicationPlan.from_json(LEGACY_V2_JSON).to_json() == LEGACY_V2_JSON


@pytest.mark.parametrize("media", [(), (MediaRef(kind="photo", file_id="a"),)])
def test_raw_env_human_labels_render_with_exact_budget_and_utf16_entities(media):
    sig = load_config({
        "POSTER_BOT_SIGNATURE_MAIN_TEXT": '\'🌲 Канал "Главный"\'',
        "POSTER_BOT_SIGNATURE_MAIN_URL": "https://t.me/main",
        "POSTER_BOT_SIGNATURE_PARTNER_TEXT": '"@канал партнера"',
        "POSTER_BOT_SIGNATURE_PARTNER_URL": "https://t.me/partner",
    }).signature
    limit = CAPTION_MAX if media else TEXT_MAX
    # Literal footer has 35 code points; its astral tree consumes two UTF-16 units.
    footer = '🌲 Канал "Главный" | @канал партнера'
    assert len(footer) == 35
    body = "A" * (limit - 37)
    plan = plan_post(LogicalPost(body=body, media=media), sig).plan
    assert plan is not None and len(plan.parts) == 1
    part = plan.parts[0]
    assert part.published_text() == body + "\n\n" + footer
    assert len(part.published_text()) == limit
    entities = part.content.media.caption_entities if media else part.content.entities
    first, second = entities[-2:]
    assert (first.offset, first.length) == (limit - 35, 18)
    assert (second.offset, second.length) == (limit - 14, 15)
    raw = part.published_text().encode("utf-16-le")
    for entity, label in zip((first, second), ('🌲 Канал "Главный"', "@канал партнера")):
        assert raw[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le") == label
    frozen = plan.to_json()
    assert PublicationPlan.from_json(frozen).to_json() == frozen


@pytest.mark.parametrize("version", [2, 3])
def test_frozen_plan_literal_outer_quotes_are_not_env_normalized(version):
    sig = SignatureConfig('"@main"', "https://t.me/main", "'@partner'", "https://t.me/partner")
    # Single-part v2/v3 differ only in the body/signature join, absent here.
    raw = plan_post(LogicalPost(media=(MediaRef(kind="photo", file_id="a"),)), sig).plan.to_json()
    if version == 2:
        data = json.loads(raw)
        data["v"] = 2
        raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
    revived = PublicationPlan.from_json(raw)
    assert revived.signature == sig
    assert revived.parts[0].published_text() == '\"@main\" | \'@partner\''
    assert revived.to_json() == raw


@pytest.mark.parametrize("version", [1, 4, True, 2.0, "3", None])
def test_unknown_or_noninteger_snapshot_versions_rejected(version):
    data = json.loads(LEGACY_V2_JSON)
    data["v"] = version
    with pytest.raises(ContentValidationError, match="version"):
        PublicationPlan.from_json(json.dumps(data))


def test_footer_format_is_validated_against_declared_snapshot_version():
    legacy = json.loads(LEGACY_V2_JSON)
    legacy["v"] = 3
    with pytest.raises(ContentValidationError):
        PublicationPlan.from_json(json.dumps(legacy))
    current = json.loads(plan_post(LogicalPost(body="A" * 3000 + NL2 + "B" * 3000), SIG).plan.to_json())
    current["v"] = 2
    with pytest.raises(ContentValidationError):
        PublicationPlan.from_json(json.dumps(current))


@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.parametrize("album", [False, True])
def test_caption_first_paragraph_exact_new_multipart_budget(extra, album):
    first = "A" * (CAPTION_MAX - total_footer_length(SIG, 2) + extra)
    body = first + NL2 + "B" * 200
    media = (MediaRef(kind="photo", file_id="a"),)
    if album:
        media += (MediaRef(kind="photo", file_id="b"),)
    plan = plan_post(LogicalPost(body=body, media=media), SIG).plan
    assert plan is not None and len(plan.parts) == 2
    if extra:
        assert plan.parts[0].published_text() == footer_text(2) + "\n\n" + SIG_TEXT
        assert plan.parts[1].published_text().startswith(body)
    else:
        assert len(plan.parts[0].published_text()) == CAPTION_MAX
        assert plan.parts[1].published_text().startswith("B" * 200)
    assert plan.reconstructed_body() == body


@pytest.mark.parametrize("own", [False, True])
def test_caption_trailing_structural_blanks_normalized_with_entities(own):
    caption = "🌲снимок\n\n\n \n"
    entities = (Entity(type="bold", offset=0, length=8),)
    item = MediaRef(kind="photo", file_id="a", caption=caption if own else None,
                    caption_entities=entities if own else ())
    logical = LogicalPost(body=None if own else caption,
                          entities=() if own else entities, media=(item,))
    part = plan_post(logical, SIG).plan.parts[0]
    assert part.published_text() == "🌲снимок\n\n" + SIG_TEXT
    assert part.content.media.caption_entities[0] == entities[0]
    assert part.content.media.caption_entities[-2:] == SIG.link_entities(10)
    assert logical.media[0].caption == (caption if own else None)


def tail(n: int) -> str:
    """Everything the planner appends below the body of part in an N-part plan."""
    if n <= 1:
        return SIG_JOIN + SIG_TEXT
    return FOOTER_JOIN + footer_text(n) + SIG_JOIN + SIG_TEXT


def empty_caption_footer(n: int) -> str:
    """Published caption of a media part whose body contributed nothing."""
    if n <= 1:
        return SIG_TEXT
    return footer_text(n) + SIG_JOIN + SIG_TEXT


def _photo(i: int, caption: str | None = None) -> MediaRef:
    return MediaRef(kind="photo", file_id=f"ph{i}", caption=caption)


class TestSignatureOnEveryPart:
    def test_single_part_signature_no_navigation(self):
        body = "первый абзац\n\nвторой абзац"
        res = plan_post(LogicalPost(body=body), SIG)
        plan = res.plan
        assert plan is not None and not res.diagnostics
        (part,) = plan.parts
        assert part.footer is None  # navigation stays multi-part only
        assert part.published_text() == body + SIG_JOIN + SIG_TEXT
        assert "Часть" not in part.published_text()
        assert part.body_slice == body  # lossless slice contract unchanged

    def test_footer_order_exact_on_every_part(self):
        p1, p2 = "A" * 3000, "B" * 3000
        body = p1 + NL2 + p2
        res = plan_post(LogicalPost(body=body), SIG)
        plan = res.plan
        assert plan is not None and len(plan.parts) == 2
        nav = footer_text(2)
        assert plan.parts[0].published_text() == p1 + FOOTER_JOIN + nav + SIG_JOIN + SIG_TEXT
        assert plan.parts[1].published_text() == p2 + FOOTER_JOIN + nav + SIG_JOIN + SIG_TEXT
        for part in plan.parts:
            assert part.published_text().endswith(SIG_JOIN + SIG_TEXT)
            assert part.published_text().endswith(tail(2))

    def test_plan_post_has_no_bypass(self):
        with pytest.raises(TypeError):
            plan_post(LogicalPost(body="текст"))
        with pytest.raises(ContentValidationError):
            plan_post(LogicalPost(body="текст"), None)  # type: ignore[arg-type]


class TestSignatureEntities:
    def _published_entities(self, part):
        if part.content.kind == "text":
            return part.content.entities
        if part.content.kind == "album":
            return part.content.caption_entities
        return part.content.media.caption_entities

    def test_signature_links_are_the_last_two_entities(self):
        body = "A" * 3000 + NL2 + "B" * 3000
        res = plan_post(LogicalPost(body=body), SIG)
        for part in res.plan.parts:
            ents = self._published_entities(part)
            first, second = ents[-2:]
            assert first.type == "text_link" and first.url == SIG.main_url
            assert second.type == "text_link" and second.url == SIG.partner_url
            total = utf16_length(part.published_text())
            assert second.offset + second.length == total  # signature is BOTTOM
            assert first.offset + first.length + utf16_length(" | ") == second.offset

    def test_signature_entities_land_on_their_labels_utf16(self):
        body = "A" * 3000 + NL2 + "B" * 3000
        res = plan_post(LogicalPost(body=body), SIG)
        for part in res.plan.parts:
            ents = self._published_entities(part)
            first, second = ents[-2:]
            raw = part.published_text().encode("utf-16-le")
            assert (
                raw[2 * first.offset : 2 * (first.offset + first.length)]
                == "@channel_main".encode("utf-16-le")
            )
            assert (
                raw[2 * second.offset : 2 * (second.offset + second.length)]
                == "@channel_partner".encode("utf-16-le")
            )

    def test_body_entities_plus_two_signature_links(self):
        body = "A" * 3000 + NL2 + "слово " + "B" * 2000
        target = body.index("слово")
        logical = LogicalPost(
            body=body,
            entities=(Entity(type="bold",
                             offset=utf16_length(body[:target]), length=5),),
        )
        res = plan_post(logical, SIG)
        assert res.plan is not None
        total = sum(len(self._published_entities(p)) for p in res.plan.parts)
        assert total == len(logical.entities) + 2 * len(res.plan.parts)

    def test_astral_signature_labels_shift_entities_correctly(self):
        tree = "\N{CHRISTMAS TREE}"
        sig = SignatureConfig(
            main_text=tree + "main", main_url="https://t.me/main",
            partner_text=tree + "partner",
            partner_url="https://t.me/partner",
        )
        body = "A" * 3000 + NL2 + "B" * 3000
        res = plan_post(LogicalPost(body=body), sig)
        for part in res.plan.parts:
            first, second = self._published_entities(part)[-2:]
            raw = part.published_text().encode("utf-16-le")
            assert (
                raw[2 * first.offset : 2 * (first.offset + first.length)]
                == (tree + "main").encode("utf-16-le")
            )
            assert (
                raw[2 * second.offset : 2 * (second.offset + second.length)]
                == (tree + "partner").encode("utf-16-le")
            )


class TestSignatureBudgets:
    def test_total_footer_length_includes_signature(self):
        assert len(SIG_TEXT) == 32
        assert total_footer_length(SIG, 1) == len(SIG_JOIN) + 32  # 34
        assert total_footer_length(SIG, 2) == (
            len(FOOTER_JOIN) + len(footer_text(2)) + len(SIG_JOIN) + 32
        )  # 2 + 17 + 2 + 32 = 53

    def test_single_part_body_at_reduced_limit_fits(self):
        body = "P" * (TEXT_MAX - len(SIG_JOIN) - 32)  # 4062: exact reduced budget
        res = plan_post(LogicalPost(body=body), SIG)
        assert res.plan is not None and len(res.plan.parts) == 1
        assert len(res.plan.parts[0].published_text()) == TEXT_MAX

    def test_paragraph_inside_bare_limit_but_over_signature_budget_manual_split(self):
        body = "P" * 4080  # fits the bare 4096 text limit, not the signature budget
        res = plan_post(LogicalPost(body=body), SIG)
        assert res.plan is None
        (diag,) = res.diagnostics
        assert diag.code == MANUAL_SPLIT_REQUIRED
        assert diag.details["length"] == 4080
        assert diag.details["budget"] == TEXT_MAX - len(SIG_JOIN) - 32
        assert "разбить" in diag.message
        assert len(body) == 4080  # never clipped, never truncated

    def test_exact_N10_dense_packing_with_signature(self):
        size = TEXT_MAX - len(FOOTER_JOIN) - 98 - len(SIG_JOIN) - 32  # 3962
        paras = ["A" * size for _ in range(10)]
        res = plan_post(LogicalPost(body=NL2.join(paras)), SIG)
        plan = res.plan
        assert plan is not None and len(plan.parts) == 10
        for part in plan.parts:
            assert len(part.published_text()) == TEXT_MAX
        assert total_footer_length(SIG, 10) == 2 + 98 + 2 + 32

    def test_caption_exact_at_reduced_budget_single_part(self):
        body = "A" * (CAPTION_MAX - len(SIG_JOIN) - 32)  # 990
        res = plan_post(LogicalPost(body=body, media=(_photo(1),)), SIG)
        plan = res.plan
        assert plan is not None and len(plan.parts) == 1
        assert plan.parts[0].published_text() == body + SIG_JOIN + SIG_TEXT
        assert len(plan.parts[0].published_text()) == CAPTION_MAX

    def test_caption_one_char_over_moves_whole_under_signature_cost(self):
        body = "A" * (CAPTION_MAX - len(SIG_JOIN) - 32 + 1)  # 991: over by one
        res = plan_post(LogicalPost(body=body, media=(_photo(1),)), SIG)
        plan = res.plan
        assert plan is not None and len(plan.parts) == 2
        media_part, text_part = plan.parts
        assert media_part.published_text() == empty_caption_footer(2)
        # the whole paragraph moves to the text part — nothing was clipped
        assert text_part.published_text() == body + tail(2)
        assert "".join(p.body_slice for p in plan.parts) == body


class TestMediaSignatures:
    def test_album_without_body_gets_one_signature_caption(self):
        res = plan_post(LogicalPost(media=(_photo(1), _photo(2))), SIG)
        plan = res.plan
        assert plan is not None and len(plan.parts) == 1
        part = plan.parts[0]
        assert part.footer is None
        assert part.content.caption == SIG_TEXT
        assert [e.type for e in part.content.caption_entities] == [
            "text_link", "text_link",
        ]

    def test_album_with_item_captions_is_a_signature_conflict(self):
        items = (_photo(1, caption="снимок"), _photo(2))
        logical = LogicalPost(media=items)
        res = plan_post(logical, SIG)
        assert res.plan is None  # blocking: the album cannot take the signature
        (diag,) = res.diagnostics
        assert diag.code == SIGNATURE_CONFLICT
        assert len(logical.media) == 2  # input preserved verbatim
        assert logical.media[0].caption == "снимок"  # never silently dropped
        # domain-accurate wording: the Bot API allows the combination, the
        # bot's own rule refuses the ambiguity — never blamed on Telegram
        assert "Telegram" not in diag.message
        assert "Bot API" in diag.message

    def test_single_media_own_caption_kept_above_signature(self):
        logical = LogicalPost(
            media=(MediaRef(kind="photo", file_id="ph1", caption="снимок",
                            caption_entities=(
                                Entity(type="bold", offset=0, length=6),),),),
        )
        res = plan_post(logical, SIG)
        plan = res.plan
        assert plan is not None and len(plan.parts) == 1
        part = plan.parts[0]
        assert part.published_text() == "снимок" + SIG_JOIN + SIG_TEXT
        ents = part.content.media.caption_entities
        assert [(e.type, e.offset, e.length) for e in ents] == [
            ("bold", 0, 6),
            ("text_link", utf16_length("снимок") + 2, 13),
            ("text_link", utf16_length("снимок") + 2 + 13 + 3, 16),
        ]

    def test_single_media_own_caption_overrun_moves_whole_to_text_part(self):
        # the CHUNK1b lossless-move correction replaced the old rejection
        # (plan=None + signature_limit_exceeded): an own caption that fits a
        # text part is never rejected — the media part publishes
        # signature-only and the WHOLE caption moves verbatim into the next
        # text part (input untouched, nothing clipped).
        logical = LogicalPost(media=(_photo(1, caption="c" * 1000),))
        res = plan_post(logical, SIG)
        assert res.plan is not None and not res.diagnostics
        media_part, text_part = res.plan.parts
        assert media_part.content.kind == "photo"
        assert media_part.published_text() == empty_caption_footer(2)
        assert text_part.content.kind == "text"
        assert text_part.published_text() == "c" * 1000 + tail(2)
        assert logical.media[0].caption == "c" * 1000  # preserved, not clipped

    def test_media_part_with_body_ends_with_signature(self):
        body = "A" * 1030 + NL2 + "B" * 3200 + NL2 + "C" * 3200
        res = plan_post(LogicalPost(body=body, media=(_photo(1), _photo(2))), SIG)
        plan = res.plan
        assert plan is not None and len(plan.parts) == 4
        assert plan.parts[0].published_text() == empty_caption_footer(4)
        for part in plan.parts[1:]:
            assert part.published_text().endswith(tail(4))


class TestSnapshotFreezesSignature:
    def _multi_plan(self):
        body = "A" * 3000 + NL2 + "B" * 3000
        return plan_post(LogicalPost(body=body), SIG).plan

    def test_snapshot_embeds_labels_and_urls_and_roundtrips(self):
        plan = self._multi_plan()
        raw = plan.to_json()
        # every label appears once per part plus once in the embedded signature
        assert raw.count("@channel_main") == 1 + len(plan.parts)
        assert "https://t.me/channel_main" in raw
        assert "https://t.me/channel_partner" in raw
        revived = PublicationPlan.from_json(raw)
        assert revived.signature == SIG  # immutable: env changes cannot reach it
        assert revived.to_json() == raw
        assert [p.published_text() for p in revived.parts] == [
            p.published_text() for p in plan.parts
        ]

    def test_different_signature_changes_the_frozen_snapshot(self):
        plan = self._multi_plan()
        other = SignatureConfig(
            main_text="@other_main", main_url="https://t.me/other_main",
            partner_text="@other_partner",
            partner_url="https://t.me/other_partner",
        )
        other_plan = plan_post(LogicalPost(body="A" * 3000 + NL2 + "B" * 3000),
                               other).plan
        assert other_plan.to_json() != plan.to_json()

    def test_plan_version_bumped_and_v1_rejected(self):
        assert PLAN_VERSION == 3
        plan = self._multi_plan()
        data = json.loads(plan.to_json())
        data["v"] = 1
        with pytest.raises(ContentValidationError, match="version"):
            PublicationPlan.from_json(json.dumps(data))

    def test_snapshot_without_signature_rejected(self):
        plan = self._multi_plan()
        data = json.loads(plan.to_json())
        data.pop("signature")
        with pytest.raises(ContentValidationError, match="signature"):
            PublicationPlan.from_json(json.dumps(data))

    def test_tampered_published_text_fails_plan_validation(self):
        plan = self._multi_plan()
        data = json.loads(plan.to_json())
        part0 = json.loads(data["parts"][0]["content_json"])
        # length-preserving swap: inner Content limits stay satisfied, so
        # only the plan-level signature check can reject this
        evil = "@evil_main_tl" + SIG_TEXT[len("@channel_main"):]
        evil = evil[:len(SIG_TEXT) - 16] + "@evil_partner_xx"
        assert len(evil) == len(SIG_TEXT)
        part0["text"] = part0["text"].replace(SIG_TEXT, evil)
        data["parts"][0]["content_json"] = json.dumps(
            part0, ensure_ascii=False, sort_keys=True)
        with pytest.raises(ContentValidationError, match="signature"):
            PublicationPlan.from_json(json.dumps(data))

    def test_shortening_tamper_rejected_even_earlier(self):
        # a shrinking swap breaks entity ranges: rejected on Content rebuild,
        # never mind the plan-level check — tampering cannot slip through
        plan = self._multi_plan()
        data = json.loads(plan.to_json())
        part0 = json.loads(data["parts"][0]["content_json"])
        part0["text"] = part0["text"].replace(SIG_TEXT, "@evil | @evil")
        data["parts"][0]["content_json"] = json.dumps(
            part0, ensure_ascii=False, sort_keys=True)
        with pytest.raises(ContentValidationError):
            PublicationPlan.from_json(json.dumps(data))


class TestFeasibilityAtConfigCaps:
    def test_maximum_size_signature_still_plans_normally(self):
        sig = SignatureConfig(
            main_text="@" + "m" * 63, main_url="https://host.example/" + "p" * 400,
            partner_text="@" + "p" * 63,
            partner_url="https://host.example/" + "q" * 400,
        )
        res = plan_post(LogicalPost(body="короткий текст"), sig)
        plan = res.plan
        assert plan is not None and len(plan.parts) == 1
        assert plan.parts[0].published_text().endswith(SIG_JOIN + sig.rendered())
        # caps keep the mandatory footer feasible inside the caption budget
        assert total_footer_length(sig, 1) < CAPTION_MAX
