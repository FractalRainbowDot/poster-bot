"""CHUNK1b caption-boundary regressions: accepted content is NEVER lost.

Confirmed defect (independent review, red before this chunk's fix):
- body + single media with its own caption: the body slice silently
  OVERWROTE the item caption ('photo caption' vanished from the plan);
- body + album whose items carry captions: ContentValidationError escaped
  plan_post as an exception instead of a PlanResult blocking diagnostic.

Encoded corrections (approved brief):
- body + captioned media is ambiguous: a LogicalPost records no arrival order
  between body paragraphs and item captions, so any merge would be a guess.
  Blocking 'item_caption_conflict' diagnostic, input preserved verbatim —
  never a merge, never an overwrite, never an exception;
- intake (composition flow) must normalize item captions into the ordered
  logical body and clear item captions BEFORE planning — the planner
  documents the contract and never guesses or duplicates;
- media-only single whose own caption cannot share the caption budget with
  the mandatory signature: signature-only media part + the WHOLE caption
  verbatim in the next text part — one paragraph, never auto-split at its
  blank lines, never clipped, caption entities preserved at their offsets;
- the album caption + per-item-caption restriction is the DOMAIN's own rule;
  the Bot API payload allows the combination — owner-facing wording states
  this accurately and never claims Telegram rejects it.
"""
from __future__ import annotations

from poster_bot.domain.content import (
    CAPTION_MAX,
    Entity,
    MediaRef,
    TEXT_MAX,
    utf16_length,
)
from poster_bot.domain.planner import (
    ALBUM_OVERFLOW,
    FOOTER_JOIN,
    ITEM_CAPTION_CONFLICT,
    SIGNATURE_CONFLICT,
    LogicalPost,
    PublicationPlan,
    footer_text,
    plan_post,
)
from poster_bot.domain.signature import SIG_JOIN, SignatureConfig

SIG = SignatureConfig(
    main_text="@channel_main", main_url="https://t.me/channel_main",
    partner_text="@channel_partner", partner_url="https://t.me/channel_partner",
)
SIG_TEXT = "@channel_main | @channel_partner"  # 32 rendered chars
NL2 = "\n\n"


def tail(n: int) -> str:
    if n <= 1:
        return SIG_JOIN + SIG_TEXT
    return FOOTER_JOIN + footer_text(n) + SIG_JOIN + SIG_TEXT


def empty_caption_footer(n: int) -> str:
    if n <= 1:
        return SIG_TEXT
    return footer_text(n) + SIG_JOIN + SIG_TEXT


def _photo(i: int, caption: str | None = None,
           entities: tuple[Entity, ...] = ()) -> MediaRef:
    return MediaRef(kind="photo", file_id=f"ph{i}", caption=caption,
                    caption_entities=entities)


class TestBodyPlusItemCaptionsIsBlocking:
    """The confirmed loss vectors: never an overwrite, never an exception."""

    def test_body_plus_single_photo_caption_blocks_never_overwrites(self):
        # red before the fix: plan existed and 'photo caption' was gone.
        body_ents = (Entity(type="bold", offset=0, length=4),)
        cap_ents = (Entity(type="bold", offset=6, length=7),)
        logical = LogicalPost(
            body="тело поста",
            entities=body_ents,
            media=(_photo(1, caption="photo caption", entities=cap_ents),),
        )
        res = plan_post(logical, SIG)  # must not raise
        assert res.plan is None  # nothing publishable, nothing guessed
        (diag,) = res.diagnostics
        assert diag.code == ITEM_CAPTION_CONFLICT
        assert diag.details["items"] == 1
        # accepted content preserved verbatim on the input
        assert logical.body == "тело поста"
        assert logical.entities == body_ents
        assert logical.media[0].caption == "photo caption"
        assert logical.media[0].caption_entities == cap_ents
        # owner-facing wording names the normalization contract + preservation
        assert "сохранено" in diag.message
        assert "до планирования" in diag.message

    def test_body_plus_album_item_captions_blocks_never_raises(self):
        # red before the fix: ContentValidationError escaped plan_post.
        logical = LogicalPost(
            body="тело поста",
            media=(_photo(1, caption="подпись 1"),
                   MediaRef(kind="video", file_id="vi2", caption="подпись 2")),
        )
        res = plan_post(logical, SIG)  # must not raise
        assert res.plan is None
        (diag,) = res.diagnostics
        assert diag.code == ITEM_CAPTION_CONFLICT
        assert diag.details["items"] == 2
        assert logical.media[0].caption == "подпись 1"
        assert logical.media[1].caption == "подпись 2"

    def test_body_plus_uncaptioned_media_still_plans(self):
        # control: the legitimate shape is untouched by the new guard
        res = plan_post(LogicalPost(body="тело поста",
                                    media=(_photo(1), _photo(2))), SIG)
        assert res.plan is not None and not res.diagnostics
        assert res.plan.parts[0].content.kind == "album"

    def test_overflow_item_caption_is_warning_covered_not_conflict(self):
        # 11th item's caption is already warning-covered ('kept on the input,
        # not published'); planned items 1..10 are uncaptioned -> normal plan
        items = tuple(_photo(i) for i in range(10)) + (
            _photo(10, caption="лишняя"),)
        logical = LogicalPost(body="тело поста", media=items)
        res = plan_post(logical, SIG)
        assert res.plan is not None
        assert any(d.code == ALBUM_OVERFLOW for d in res.plan.diagnostics)
        assert logical.media[10].caption == "лишняя"  # preserved on the input


class TestDomainAccurateWording:
    def test_album_conflict_message_never_blames_telegram(self):
        # the Bot API allows an album caption next to per-item captions; the
        # rejection is the bot's own domain rule (Content._check_album).
        res = plan_post(LogicalPost(
            media=(_photo(1, caption="снимок"), _photo(2))), SIG)
        assert res.plan is None
        (diag,) = res.diagnostics
        assert diag.code == SIGNATURE_CONFLICT  # stable machine key
        assert "Telegram" not in diag.message
        assert "Bot API" in diag.message
        assert "сохранено" in diag.message


class TestSingleCaptionLosslessMove:
    """Media-only single: own caption over the signature-reduced caption
    budget moves WHOLE into a following text part instead of being rejected."""

    def test_caption_over_budget_moves_whole_to_text_part(self):
        caption = "c" * 1000  # +1+32 breaches CAPTION_MAX; fits a text part
        logical = LogicalPost(media=(_photo(1, caption=caption),))
        res = plan_post(logical, SIG)
        assert res.plan is not None and not res.diagnostics
        assert logical.media[0].caption == caption  # input untouched
        media_part, text_part = res.plan.parts
        assert media_part.content.kind == "photo"
        assert media_part.published_text() == empty_caption_footer(2)
        assert media_part.footer is not None  # 2-part plan: nav everywhere
        assert text_part.content.kind == "text"
        assert text_part.published_text() == caption + tail(2)
        assert len(text_part.published_text()) <= TEXT_MAX
        # no logical body exists: slices still join to ''
        assert res.plan.reconstructed_body() == ""

    def test_moved_caption_entities_survive_verbatim(self):
        url = "https://example.com/a"
        prefix = "\U0001f384снимок "  # astral head: UTF-16 offsets drift
        caption = prefix + url + "c" * 990  # 1021 chars: over caption budget
        cap_ents = (
            Entity(type="bold", offset=0, length=2),  # the astral char
            Entity(type="link", offset=utf16_length(prefix), length=len(url),
                   url=url),
        )
        logical = LogicalPost(media=(_photo(1, caption, cap_ents),))
        res = plan_post(logical, SIG)
        plan = res.plan
        assert plan is not None
        text_part = plan.parts[1]
        assert text_part.content.entities[:2] == cap_ents  # offsets unchanged
        first, second = text_part.content.entities[-2:]
        assert (first.type, first.url) == ("text_link", SIG.main_url)
        assert (second.type, second.url) == ("text_link", SIG.partner_url)
        published = text_part.published_text()
        assert published.startswith(caption)  # whole, verbatim
        raw = published.encode("utf-16-le")
        assert raw[2 * cap_ents[0].offset:2 * 2] == "\U0001f384".encode(
            "utf-16-le")
        assert (raw[2 * cap_ents[1].offset:
                    2 * (cap_ents[1].offset + cap_ents[1].length)]
                == url.encode("utf-16-le"))

    def test_multi_paragraph_caption_never_auto_split(self):
        # the moved caption stays ONE paragraph in the destination part even
        # though it contains blank lines the packer would otherwise cut at
        caption = "A" * 500 + NL2 + "B" * 501
        logical = LogicalPost(
            media=(MediaRef(kind="video", file_id="vi9", caption=caption),))
        res = plan_post(logical, SIG)
        plan = res.plan
        assert plan is not None
        assert len(plan.parts) == 2  # exactly one destination text part
        assert plan.parts[1].published_text().startswith(caption)

    def test_caption_exactly_at_budget_stays_on_media(self):
        # boundary: the move only fires when the caption + signature exceed
        # the caption budget — an exact fit still rides the media part
        caption = "A" * (CAPTION_MAX - len(SIG_JOIN) - len(SIG_TEXT))  # 991
        res = plan_post(LogicalPost(media=(_photo(1, caption),)), SIG)
        plan = res.plan
        assert plan is not None and len(plan.parts) == 1
        assert plan.parts[0].published_text() == caption + SIG_JOIN + SIG_TEXT
        assert plan.parts[0].footer is None

    def test_moved_plan_snapshot_roundtrips(self):
        caption = "c" * 1000
        plan = plan_post(LogicalPost(media=(_photo(1, caption),)), SIG).plan
        assert plan is not None
        raw = plan.to_json()
        assert raw == plan.to_json()  # deterministic key order
        revived = PublicationPlan.from_json(raw)
        assert revived.to_json() == raw
        assert revived.parts[1].published_text().startswith(caption)