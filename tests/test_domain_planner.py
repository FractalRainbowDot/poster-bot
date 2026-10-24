"""CHUNK1+CHUNK1b planner acceptance: LogicalPost -> frozen PublicationPlan.

Encoded decisions (approved brief):
- greedy packing of WHOLE paragraphs, contiguous order, no reordering;
- footer 'Часть 1 | Часть 2 | ...' only for multi-part plans; current part is
  plain text, siblings are link slots; footer budget counts rendered labels
  (URLs are entity payloads, not message characters — verified Bot API);
- the MANDATORY signature is the bottom line of EVERY part (one blank line
  below the body/navigation); plan_post requires a SignatureConfig — there is
  no bypass; budgets include navigation + signature + their joins;
- budgets: text 4096 / caption 1024 INCLUDE the joins, footer and signature;
  footer length depends on the final part count N (digit growth 9->10),
  converged deterministically, never worst-case over-reservation;
- paragraph that misses the caption but fits a text part moves whole;
- a block that cannot fit even the smallest multi-part footer budget ->
  MANUAL_SPLIT_REQUIRED diagnostic: no fragments, input preserved verbatim;
- media part is first; nav footer rides its caption even with no body text;
  single album has no nav footer but DOES carry the signature caption;
  11th album item warns, first 10 kept, the rest preserved on the input
  (never silently lost, never silently published);
- preview stack == frozen publication stack: parts order + published text.
"""
from __future__ import annotations

from poster_bot.domain.content import CAPTION_MAX, MediaRef, TEXT_MAX
from poster_bot.domain.planner import (
    FOOTER_JOIN,
    FOOTER_SEP,
    MANUAL_SPLIT_REQUIRED,
    ALBUM_OVERFLOW,
    FOOTER_TOO_LARGE,
    LogicalPost,
    footer_length,
    footer_text,
    plan_post,
    total_footer_length,
)
from poster_bot.domain.signature import SIG_JOIN, SignatureConfig

NL2 = "\n\n"

SIG = SignatureConfig(
    main_text="@channel_main", main_url="https://t.me/channel_main",
    partner_text="@channel_partner", partner_url="https://t.me/channel_partner",
)
SIG_TEXT = "@channel_main | @channel_partner"
assert SIG.length() == len(SIG_TEXT) == 32


def _tail(n: int) -> str:
    """Everything below the body in a part of an N-part plan."""
    if n <= 1:
        return SIG_JOIN + SIG_TEXT
    return FOOTER_JOIN + footer_text(n) + SIG_JOIN + SIG_TEXT


def _photo(i: int) -> MediaRef:
    return MediaRef(kind="photo", file_id=f"ph{i}")


def _video(i: int) -> MediaRef:
    return MediaRef(kind="video", file_id=f"vi{i}")


def _body(*paras: str) -> str:
    return NL2.join(paras)


class TestFooterMath:
    def test_footer_format_and_lengths(self):
        assert footer_text(2) == "Часть 1 | Часть 2"
        assert footer_length(1) == 0  # single part: no navigation at all
        assert footer_length(2) == len("Часть 1 | Часть 2")
        # digit growth boundary 9 -> 10 (documented literals)
        assert footer_length(9) == 87
        assert footer_length(10) == 98
        assert FOOTER_SEP == " | "
        assert FOOTER_JOIN == NL2

    def test_total_footer_includes_signature(self):
        # Both body/navigation and navigation/signature joins are blank lines.
        assert total_footer_length(SIG, 1) == 2 + 32
        assert total_footer_length(SIG, 2) == 17 + 2 + 2 + 32
        assert total_footer_length(SIG, 10) == 98 + 2 + 2 + 32


class TestSinglePartNoFooter:
    def test_short_text_one_part_verbatim_no_navigation(self):
        logical = LogicalPost(body="первый абзац\n\nвторой абзац")
        res = plan_post(logical, SIG)
        assert res.plan is not None and not res.diagnostics
        (part,) = res.plan.parts
        assert part.footer is None
        assert part.published_text() == logical.body + SIG_JOIN + SIG_TEXT
        assert part.body_slice == logical.body

    def test_trailing_blanklines_kept_in_slice_not_published_before_signature(self):
        body = "текст\n\n\n"
        res = plan_post(LogicalPost(body=body), SIG)
        part = res.plan.parts[0]
        assert part.published_text() == "текст" + SIG_JOIN + SIG_TEXT
        assert part.content.text == "текст" + SIG_JOIN + SIG_TEXT
        assert part.body_slice == body


class TestMultiPartPacking:
    def test_two_parts_footer_crosslinks(self):
        p1, p2 = "A" * 3000, "B" * 3000
        body = _body(p1, p2)
        res = plan_post(LogicalPost(body=body), SIG)
        plan = res.plan
        assert plan is not None and len(plan.parts) == 2
        f = footer_text(2)
        part1, part2 = plan.parts
        assert part1.published_text() == p1 + FOOTER_JOIN + f + SIG_JOIN + SIG_TEXT
        assert part2.published_text() == p2.rstrip("\n") + FOOTER_JOIN + f + SIG_JOIN + SIG_TEXT
        # lossless: verbatim slices join back into the original body
        assert "".join(p.body_slice for p in plan.parts) == body
        # current label plain, sibling linked
        slots1 = {s.part_index: s for s in part1.footer.slots}
        assert slots1[1].linked is False and slots1[2].linked is True
        slots2 = {s.part_index: s for s in part2.footer.slots}
        assert slots2[1].linked is True and slots2[2].linked is False

    def test_greedy_packs_max_paragraphs_at_final_footer(self):
        paras = ["A" * 2000, "B" * 2000, "C" * 2000]
        res = plan_post(LogicalPost(body=_body(*paras)), SIG)
        plan = res.plan
        # exact converged N=2 footer (not a worst-case 3-part reservation):
        # two paragraphs fit in part 1 at final-N budget.
        assert len(plan.parts) == 2
        f = footer_text(2)
        assert plan.parts[0].published_text() == (
            paras[0] + NL2 + paras[1] + FOOTER_JOIN + f + SIG_JOIN + SIG_TEXT
        )
        assert plan.parts[1].published_text() == (
            paras[2] + FOOTER_JOIN + f + SIG_JOIN + SIG_TEXT
        )
        assert len(plan.parts[0].published_text()) <= TEXT_MAX
        # maximality: the next block no longer fits part 1 at final N
        over = paras[0] + NL2 + paras[1] + NL2 + paras[2]
        assert len(over) + total_footer_length(SIG, 2) > TEXT_MAX

    def test_footer_digit_growth_9_to_10_convergence(self):
        paras = [str(i) + "x" * 3961 for i in range(10)]  # 3962 each
        # TEXT_MAX - blank line - nav(10) - blank line - signature exactly
        assert len(paras[0]) == TEXT_MAX - len(FOOTER_JOIN) - footer_length(10) \
            - len(SIG_JOIN) - SIG.length() == 3962
        res = plan_post(LogicalPost(body=_body(*paras)), SIG)
        plan = res.plan
        assert plan is not None and len(plan.parts) == 10
        for idx, part in enumerate(plan.parts, start=1):
            assert part.index == idx
            assert len(part.published_text()) == TEXT_MAX  # exact-N budget
        assert plan.parts[0].footer.text == footer_text(10)
        assert footer_length(10) == 98  # 87 would be wrong (9-digit footer)

    def test_trailing_blanklines_stripped_before_footer_but_saved_in_slice(self):
        p1, p2 = "A" * 3000, "B" * 3000
        body = p1 + NL2 + p2 + "\n\n\n"
        res = plan_post(LogicalPost(body=body), SIG)
        plan = res.plan
        assert len(plan.parts) == 2
        last = plan.parts[1]
        assert last.body_slice == p2 + "\n\n\n"  # verbatim for lossless join
        assert last.published_text().endswith(
            footer_text(2) + SIG_JOIN + SIG_TEXT)
        assert not last.published_text().removesuffix(
            FOOTER_JOIN + footer_text(2) + SIG_JOIN + SIG_TEXT).endswith("\n")
        assert "".join(p.body_slice for p in plan.parts) == body


class TestCaptionBudgetAndMovement:
    def test_whole_body_fits_caption_single_part_no_footer(self):
        body = "A" * (CAPTION_MAX - len(SIG_JOIN) - SIG.length())  # 990
        res = plan_post(LogicalPost(body=body, media=(_photo(1),)), SIG)
        assert res.plan is not None and len(res.plan.parts) == 1
        part = res.plan.parts[0]
        assert part.content.kind == "photo"
        assert part.content.media.caption == body + SIG_JOIN + SIG_TEXT
        assert len(part.content.media.caption) == CAPTION_MAX  # exact fit
        assert part.footer is None

    def test_paragraph_over_caption_but_fits_text_moves_whole(self):
        p1 = "A" * 1030  # > caption budget incl. footer+sig, < text budget
        p2 = "B" * 100
        res = plan_post(LogicalPost(body=_body(p1, p2), media=(_photo(1),)), SIG)
        plan = res.plan
        assert plan is not None and len(plan.parts) == 2
        media_part, text_part = plan.parts
        assert media_part.content.kind == "photo"
        # first paragraph does not fit the caption budget -> empty caption
        # except footer+signature; paragraph moves whole into the TEXT part.
        assert media_part.published_text() == footer_text(2) + SIG_JOIN + SIG_TEXT
        assert text_part.published_text() == (
            p1 + NL2 + p2 + FOOTER_JOIN + footer_text(2) + SIG_JOIN + SIG_TEXT)
        assert "".join(p.body_slice for p in plan.parts) == _body(p1, p2)

    def test_caption_prefix_then_rest_in_text_parts(self):
        p1, p2 = "A" * 900, "B" * 300  # p1 fits caption budget 971 at N=2
        res = plan_post(LogicalPost(body=_body(p1, p2), media=(_photo(1),)), SIG)
        plan = res.plan
        assert len(plan.parts) == 2
        assert plan.parts[0].published_text() == (
            p1 + FOOTER_JOIN + footer_text(2) + SIG_JOIN + SIG_TEXT)
        assert plan.parts[1].published_text() == (
            p2 + FOOTER_JOIN + footer_text(2) + SIG_JOIN + SIG_TEXT)
        assert len(plan.parts[0].published_text()) <= CAPTION_MAX

    def test_no_reorder_caption_never_skips_first_paragraph(self):
        p1, p2 = "A" * 1030, "B" * 20  # p1 cannot enter the caption
        res = plan_post(LogicalPost(body=_body(p1, p2), media=(_photo(1),)), SIG)
        plan = res.plan
        # order preserved: p1 is the head of the first TEXT part, not dropped
        # into the album caption later and not reordered.
        assert plan.parts[1].published_text().startswith(p1)
        assert p2 in plan.parts[1].published_text()


class TestOversizedAndFooterLimits:
    def test_single_block_over_text_limit_manual_split_no_fragments(self):
        huge = "Z" * (TEXT_MAX + 400)
        res = plan_post(LogicalPost(body=huge), SIG)
        assert res.plan is None
        (diag,) = res.diagnostics
        assert diag.code == MANUAL_SPLIT_REQUIRED
        assert "разбить" in diag.message
        assert diag.details["length"] == TEXT_MAX + 400
        # budget already accounts for the mandatory signature line
        assert diag.details["budget"] == TEXT_MAX - total_footer_length(SIG, 1)
        assert len(huge) == TEXT_MAX + 400  # input untouched, no fragments

    def test_footer_too_large_is_bounded_clear_rejection(self):
        paras = ["a" + "b" * 3989 for _ in range(420)]  # 3990 each: one per part
        res = plan_post(LogicalPost(body=_body(*paras)), SIG)
        assert res.plan is None
        (diag,) = res.diagnostics
        assert diag.code == FOOTER_TOO_LARGE
        assert diag.details["parts"] >= 420  # bounded: real N, not a guess
        assert diag.details["footer_length"] > TEXT_MAX


class TestMediaAlbum:
    def test_album_order_preserved_footer_on_first_caption(self):
        items = (_photo(1), _video(2), _photo(3))
        body = _body("A" * 3000, "B" * 3000)  # forces N=3
        res = plan_post(LogicalPost(body=body, media=items), SIG)
        plan = res.plan
        assert len(plan.parts) == 3
        first = plan.parts[0]
        assert first.content.kind == "album"
        assert tuple(m.file_id for m in first.content.items) == ("ph1", "vi2", "ph3")
        # no body fit the caption: footer + signature remain
        assert first.content.caption == footer_text(3) + SIG_JOIN + SIG_TEXT
        assert plan.parts[1].content.kind == "text"
        assert plan.parts[2].content.kind == "text"
        assert "".join(p.body_slice for p in plan.parts) == body

    def test_single_album_eleven_items_warn_keep_ten_input_preserved(self):
        items = tuple(_photo(i) for i in range(11))
        logical = LogicalPost(media=items)
        res = plan_post(logical, SIG)
        assert len(logical.media) == 11  # input never mutated/truncated
        plan = res.plan
        assert plan is not None and len(plan.parts) == 1
        (diag,) = plan.diagnostics
        assert diag.code == ALBUM_OVERFLOW
        assert diag.details["received"] == 11
        assert diag.details["kept"] == 10
        assert tuple(m.file_id for m in plan.parts[0].content.items) == tuple(
            f"ph{i}" for i in range(10)
        )
        # 11th item is NOT published and NOT silently lost: it stays on the
        # preserved LogicalPost input for the owner-visible flow (CHUNK2).
        assert logical.media[10].file_id == "ph10"

    def test_single_album_no_footer_no_body_still_signed(self):
        res = plan_post(LogicalPost(media=(_photo(1), _video(2))), SIG)
        plan = res.plan
        assert len(plan.parts) == 1 and plan.parts[0].footer is None
        # media-only albums get the signature as their caption (CHUNK1b)
        assert plan.parts[0].content.caption == SIG_TEXT

    def test_media_without_body_overflow_does_not_add_parts(self):
        # overflow warning alone never creates extra parts
        items = tuple(_video(i) for i in range(12))
        res = plan_post(LogicalPost(media=items), SIG)
        assert len(res.plan.parts) == 1


class TestPreviewStack:
    def test_preview_equals_frozen_stack_order(self):
        body = _body("A" * 1030, "B" * 3200, "C" * 3200)
        res = plan_post(LogicalPost(body=body, media=(_photo(1), _photo(2))), SIG)
        plan = res.plan
        assert [p.content.kind for p in plan.parts] == ["album", "text", "text", "text"]
        f = footer_text(4)
        sig_line = SIG_JOIN + SIG_TEXT
        assert [p.published_text() for p in plan.parts] == [
            f + sig_line,
            "A" * 1030 + FOOTER_JOIN + f + sig_line,
            "B" * 3200 + FOOTER_JOIN + f + sig_line,
            "C" * 3200 + FOOTER_JOIN + f + sig_line,
        ]
