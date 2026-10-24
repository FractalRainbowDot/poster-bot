"""CHUNK1+CHUNK1b domain planner — entity integrity, serialization, purity,
properties.

Companion to tests/test_domain_planner.py (packing/footer acceptance). This
file owns the UTF-16 entity story, snapshot compatibility, import purity and
hypothesis invariants. Every part additionally carries the MANDATORY signature
as its bottom line plus two trailing 'text_link' entities. Domain-only: no
transport, DB, or asyncio anywhere.
"""
import ast
import json
import pathlib

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from poster_bot.domain.content import (
    Content,
    Entity,
    MediaRef,
    utf16_length,
)
from poster_bot.domain.planner import (
    FOOTER_JOIN,
    MANUAL_SPLIT_REQUIRED,
    FOOTER_TOO_LARGE,
    LogicalPost,
    PublicationPlan,
    footer_length,
    footer_text,
    plan_post,
)
from poster_bot.domain.signature import SIG_JOIN, SignatureConfig

SIG = SignatureConfig(
    main_text="@channel_main", main_url="https://t.me/channel_main",
    partner_text="@channel_partner", partner_url="https://t.me/channel_partner",
)
SIG_TEXT = "@channel_main | @channel_partner"


def u16_slice(text: str, offset: int, length: int) -> str:
    """Substring of ``text`` selected by UTF-16 code-unit range, the same
    coordinate space Telegram entities use."""
    raw = text.encode("utf-16-le")[2 * offset : 2 * (offset + length)]
    return raw.decode("utf-16-le", errors="ignore")


def big_paragraph(char: str, size: int) -> str:
    return char * size


# ---------------------------------------------------------------------------
# entity shifting into the part's own UTF-16 space
# ---------------------------------------------------------------------------


def test_entities_shift_into_part_utf16_space_with_astral_prefix():
    # Part 1 is astral-heavy so global UTF-16 offsets drift from character
    # offsets; the entity living in part 2 must arrive shifted into part 2's
    # own coordinate space, still pointing at the exact word.
    p1 = "a" * 3980 + "\U0001f384" * 20  # 4000 chars, 4020 UTF-16 units
    p2 = "текст " * 20 + "ВАЖНО " + "хвост"  # pushes the total over 4096
    target = p2.index("ВАЖНО")
    logical = LogicalPost(
        body=p1 + "\n\n" + p2,
        entities=(
            Entity(
                type="bold",
                offset=utf16_length(p1 + "\n\n") + target,
                length=5,
            ),
        ),
    )
    result = plan_post(logical, SIG)
    assert result.plan is not None and len(result.plan.parts) == 2
    part2 = result.plan.parts[1]
    published = part2.published_text()
    assert published is not None
    # body entity first, the two signature links trail after it
    assert len(part2.content.entities) == 3
    ent = part2.content.entities[0]
    # global offset would be 4020 + 2 + target; part-local must be target
    assert ent.offset == target
    assert u16_slice(published, ent.offset, ent.length) == "ВАЖНО"


def test_link_entity_boundary_survives_split():
    url = "https://example.com/report"
    p1 = big_paragraph("з", 4000)
    p2 = f"отчёт: {url} — смотрите до конца" + big_paragraph("!", 60)
    logical = LogicalPost(
        body=p1 + "\n\n" + p2,
        entities=(
            Entity(
                type="link",
                offset=utf16_length(p1 + "\n\n") + p2.index("http"),
                length=len(url),
                url=url,
            ),
        ),
    )
    result = plan_post(logical, SIG)
    assert result.plan is not None and len(result.plan.parts) == 2
    part2 = result.plan.parts[1]
    published = part2.published_text()
    assert len(part2.content.entities) == 3  # body entity + two signature links
    ent = part2.content.entities[0]
    assert ent.url == url
    assert u16_slice(published, ent.offset, ent.length) == url


def test_custom_emoji_entity_shifts_with_astral_in_own_part():
    emoji = "\U0001faf6"  # astral: 1 code point, 2 UTF-16 units
    p1 = big_paragraph("b", 3990)
    p2 = f"жест {emoji} и текст дальше " + "д" * 105
    logical = LogicalPost(
        body=p1 + "\n\n" + p2,
        entities=(
            Entity(
                type="custom_emoji",
                offset=utf16_length(p1 + "\n\n" + p2[: p2.index(emoji)]),
                length=utf16_length(emoji),
                custom_emoji_id="5",
            ),
        ),
    )
    result = plan_post(logical, SIG)
    part2 = result.plan.parts[1]
    published = part2.published_text()
    assert len(part2.content.entities) == 3  # body entity + two signature links
    ent = part2.content.entities[0]
    assert u16_slice(published, ent.offset, ent.length) == emoji


# ---------------------------------------------------------------------------
# entities lock seams; oversized locked blocks ask for manual split
# ---------------------------------------------------------------------------


def test_entity_spanning_separator_locks_paragraphs_into_one_part():
    p1 = "x" * 1800 + "ХВОСТ"
    p2 = "НАЧАЛО" + "y" * 1794
    p3 = "z" * 1000
    sep = "\n\n"
    body = p1 + sep + p2 + sep + p3
    start_u16 = utf16_length(p1) - len("ХВОСТ")
    length = utf16_length("ХВОСТ" + sep + "НАЧАЛО")
    logical = LogicalPost(
        body=body,
        entities=(Entity(type="bold", offset=start_u16, length=length),),
    )
    result = plan_post(logical, SIG)
    assert result.plan is not None
    parts = result.plan.parts
    assert len(parts) == 2
    # the separator the entity spans stays intact inside part 1
    assert p1 + sep + p2 in parts[0].body_slice
    assert parts[1].body_slice.startswith(p3)
    published1 = parts[0].published_text()
    assert len(parts[0].content.entities) == 3  # lock entity + two signature links
    ent = parts[0].content.entities[0]
    assert u16_slice(published1, ent.offset, ent.length) == "ХВОСТ" + sep + "НАЧАЛО"


def test_locked_oversized_block_requests_manual_split():
    p1 = "А" * 2200
    p2 = "Б" * 2000
    body = p1 + "\n\n" + p2
    span_len = utf16_length("AAAAА\n\nБББББ")
    logical = LogicalPost(
        body=body,
        entities=(Entity(type="bold", offset=2195, length=span_len),),
    )
    result = plan_post(logical, SIG)
    assert result.plan is None  # no fragments, nothing published
    (diag,) = result.diagnostics
    assert diag.code == MANUAL_SPLIT_REQUIRED
    assert "разбить" in diag.message
    assert diag.details["length"] > diag.details["budget"]


def test_footer_overflow_reports_real_numbers_not_guess():
    body = "\n\n".join(big_paragraph("п", 3989) for _ in range(420))
    result = plan_post(LogicalPost(body=body), SIG)
    assert result.plan is None
    (diag,) = result.diagnostics
    assert diag.code == FOOTER_TOO_LARGE
    assert diag.details["footer_length"] > 4096
    assert diag.details["parts"] >= 2


# ---------------------------------------------------------------------------
# navigation slots: current part unlinked, labels exact in UTF-16 space
# ---------------------------------------------------------------------------


def test_current_part_link_excluded_and_slots_land_on_labels():
    body = "\n\n".join(
        big_paragraph(ch, 2000) + ("\U0001f384" * 7 if i % 2 else "")
        for i, ch in enumerate("abc")
    )
    result = plan_post(LogicalPost(body=body), SIG)
    plan = result.plan
    assert plan is not None and len(plan.parts) >= 2
    n = len(plan.parts)
    ftext = footer_text(n)
    for part in plan.parts:
        published = part.published_text()
        assert published is not None and published.endswith(
            FOOTER_JOIN + ftext + SIG_JOIN + SIG_TEXT)
        footer = part.footer
        assert footer is not None and footer.text == ftext
        plain = [s for s in footer.slots if not s.linked]
        assert len(plain) == 1 and plain[0].part_index == part.index
        slots = sorted(footer.slots, key=lambda s: s.utf16_offset)
        for i, slot in enumerate(slots, start=1):
            assert slot.part_index == i
            assert u16_slice(published, slot.utf16_offset, slot.utf16_length) == (
                f"Часть {i}"
            )
        # no overlap, ascending
        for a, b in zip(slots, slots[1:]):
            assert a.utf16_offset + a.utf16_length <= b.utf16_offset


def test_astral_4063_codepoint_body_accepted_without_utf16_inflation():
    # 4063 astral code points: 8126 UTF-16 units. The domain budget counts
    # code points (existing decision), so body + '\\n' + signature line is
    # exactly 4096 characters and must publish as ONE part.
    body = "\U0001f384" * (4096 - len(SIG_JOIN) - len(SIG_TEXT))  # 4063
    result = plan_post(LogicalPost(body=body), SIG)
    assert result.plan is not None
    (part,) = result.plan.parts
    assert part.content.kind == "text"
    assert part.content.text == body + SIG_JOIN + SIG_TEXT
    assert len(part.content.text) == 4096
    assert part.footer is None
    assert part.body_slice == body  # slice stays verbatim; footer is appended


# ---------------------------------------------------------------------------
# snapshot serialization: roundtrip, determinism, old-Content compatibility
# ---------------------------------------------------------------------------


def _rich_logical() -> LogicalPost:
    url = "https://example.com/x"
    p1 = "отчёт " + "д" * 900
    p2 = "вторая " + "е" * 900
    p3 = f"третья часть с {url} внутри"
    body = p1 + "\n\n" + p2 + "\n\n" + p3
    media = tuple(
        MediaRef(kind="photo", file_id=f"file{i}") for i in range(12)
    )  # 12 > 10: album_overflow warning must survive serialization
    entities = (
        Entity(type="bold", offset=0, length=6),
        Entity(type="link", offset=utf16_length(p1) + 2 + utf16_length(p2) + 2 + p3.index("http"), length=len(url), url=url),
    )
    return LogicalPost(body=body, entities=entities, media=media)


def test_publication_plan_json_roundtrip_and_determinism():
    logical = _rich_logical()
    result = plan_post(logical, SIG)
    plan = result.plan
    assert plan is not None
    assert any(d.code == "album_overflow" for d in plan.diagnostics)
    raw = plan.to_json()
    assert raw == plan.to_json()  # deterministic key order
    revived = PublicationPlan.from_json(raw)
    assert revived.to_json() == raw
    assert [p.published_text() for p in revived.parts] == [
        p.published_text() for p in plan.parts
    ]
    assert revived.reconstructed_body() == logical.body
    assert revived.signature == plan.signature  # frozen signature survives too
    assert [d.code for d in revived.diagnostics] == [d.code for d in plan.diagnostics]


def test_logical_post_roundtrip_preserves_body_entities_media():
    logical = _rich_logical()
    revived = LogicalPost.from_json(logical.to_json())
    assert revived.body == logical.body
    assert revived.entities == logical.entities
    assert revived.media == logical.media
    assert revived.to_json() == logical.to_json()


def test_old_content_json_compat_untouched_and_embedding_is_string_level():
    old = Content(
        kind="text",
        text="Привет, мир",
        entities=(Entity(type="bold", offset=0, length=6),),
    )
    assert Content.from_json(old.to_json()) == old  # existing contract intact
    result = plan_post(LogicalPost(body="A" * 3900 + "\n\n" + "B" * 300), SIG)
    part = result.plan.parts[0]
    assert isinstance(part.to_json()["content_json"], str)
    assert Content.from_json(part.to_json()["content_json"]) == part.content
    # embedding reuses Content's own untouched serialization byte-for-byte
    assert (
        json.loads(part.to_json()["content_json"])
        == json.loads(part.content.to_json())
    )


def test_planner_is_domain_pure_no_async_db_or_ui_imports():
    source = pathlib.Path(
        "src/poster_bot/domain/planner.py"
    ).read_text(encoding="utf-8")
    allowed_stdlib = {"__future__", "dataclasses", "json", "re", "typing"}
    allowed_relative = {"content", "errors", "signature"}  # same-package domain modules only
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in allowed_stdlib, alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if node.level > 0:
                assert mod in allowed_relative, node.module
            else:
                assert mod in allowed_stdlib, node.module
            assert "asyncio" not in (node.module or "")
            assert "transport" not in (node.module or "")
            assert "ui" not in (node.module or "")


# ---------------------------------------------------------------------------
# hypothesis: lossless joins, hard limits, determinism on arbitrary bodies
# ---------------------------------------------------------------------------

_BODY_ALPHABET = st.text(
    alphabet=st.sampled_from("ab \n\U0001f384",), min_size=1, max_size=300
)


def _nonblank_body() -> st.SearchStrategy:
    return _BODY_ALPHABET.filter(lambda s: bool(s.strip()) and "\n\n\n" not in s)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
@given(body=_nonblank_body())
def test_property_lossless_join_limits_and_determinism(body: str):
    logical = LogicalPost(body=body)
    first = plan_post(logical, SIG)
    second = plan_post(logical, SIG)
    if first.plan is None:
        assert second.plan is None
        assert first.diagnostics and all(
            d.code in (MANUAL_SPLIT_REQUIRED, FOOTER_TOO_LARGE)
            for d in first.diagnostics
        )
        return
    plan = first.plan
    n = len(plan.parts)
    assert "".join(p.body_slice for p in plan.parts) == body
    for part in plan.parts:
        published = part.published_text()
        if part.content.kind == "text":
            assert len(part.content.text) <= 4096
        if published is not None:
            assert len(published) <= 1024 or part.content.kind == "text"
            assert published.endswith(SIG_JOIN + SIG_TEXT)  # mandatory line
        if n == 1:
            assert part.footer is None
        else:
            assert part.footer is not None and part.footer.text == footer_text(n)
    assert plan.to_json() == second.plan.to_json()


@settings(max_examples=40, deadline=None)
@given(n=st.integers(min_value=2, max_value=500))
def test_property_footer_is_monotone_and_exact(n: int):
    text = footer_text(n)
    assert text.count("Часть") == n
    assert footer_length(n) == len(text)
    if n > 2:
        assert footer_length(n) >= footer_length(n - 1)


def test_plan_result_blocks_and_warnings_are_disjoint():
    result = plan_post(_rich_logical(), SIG)
    assert result.plan is not None
    assert all(d.code == "album_overflow" for d in result.diagnostics)
    blocked = plan_post(LogicalPost(body="А" * 2200 + "\n\n" + "Б" * 2200,
                                    entities=(Entity(type="bold", offset=2195,
                                                     length=utf16_length("ААААА\n\nБББББ")),)),
                       SIG)
    assert blocked.plan is None
    assert blocked.diagnostics[0].code == MANUAL_SPLIT_REQUIRED