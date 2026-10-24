"""Pure publication planner: LogicalPost -> frozen multi-part plan (CHUNK1;
mandatory signature footer CHUNK1b).

Design decisions encoded here (approved brief):
- one logical post becomes ONE logical scheduled publication: parts publish
  for the same accepted deadline; no scheduler/publishing side effects live
  in this module — it is pure data in, data out;
- long text splits at BLANK-LINE paragraph boundaries only; WHOLE paragraphs
  are greedily packed in contiguous order. A paragraph never gets cut: if a
  paragraph (or entity-locked block of paragraphs) cannot fit a text part
  even with the smallest multi-part footer, the planner returns a
  MANUAL_SPLIT_REQUIRED diagnostic and preserves the input verbatim;
- seam eligibility: a blank-line separator may become a part boundary only if
  no entity spans any of its UTF-16 units — an entity can never be cut and
  can never fall into the unpublished whitespace at a seam;
- multi-part plans carry a navigation footer 'Часть 1 | Часть 2 | ...' on
  EVERY part; the current part's label is plain text, sibling labels are
  link slots (URLs arrive only after publish receipts, CHUNK3). Budgets
  count rendered footer text (labels + separators), never HTML bytes or
  URLs: Bot API 'sendMessage' counts '1-4096 characters after entities
  parsing' (verified 2026-10-22 against core.telegram.org);
- the MANDATORY signature '@main | @partner' (domain.signature.SignatureConfig
  — required plan_post argument, no bypass) is the BOTTOM line of every
  published text part, single-media caption and album caption. Footer order:
   body, blank line, navigation (multi-part only), blank line, signature.
  Single-part plans carry the signature but no navigation. Signature links
  ride as 'text_link' entities over the plain labels — URLs live in entities
  and never count toward the character budget; rendered label characters do;
- footer length depends on the final part count N (digit growth 9 -> 10).
  N is found by deterministic convergence: packing budgets use the EXACT
  footer of the final N, so parts pack the MAXIMUM number of paragraphs at
  final N — no worst-case reservation, no fixed point removed by guessing;
  the fixed point is recomputed with the signature budget on both media and
  text parts;
- budgets: text 4096 / caption 1024 include the navigation footer, the
  mandatory signature and their joins;
  media (album/single) is the FIRST part; caption takes the leading
  paragraphs that fit its budget, the rest move whole to TEXT parts (a
  paragraph over the caption budget but inside the text budget is moved,
  never rejected). An album's 11th+ item produces a warning diagnostic:
  the first 10 are planned, the rest stay preserved on the input;
- lossless body: every part records its verbatim source slice; joining all
  slices reconstructs the owner's body character-for-character, including
  blank lines and leading/trailing whitespace. Only the blank-line run at a
  seam and (when footer-joining) the trailing blank run of a part are
  omitted from the PUBLISHED rendering — they remain in the snapshot slices;
- media caption boundary (CHUNK1b correction, 'never lose accepted content'):
  the planner NEVER merges an aggregated body with an item's own caption and
  never lets the body overwrite a caption. A LogicalPost records no arrival
  order between body paragraphs and item captions, so any merge would be a
  guess: such un-normalized combinations return a blocking
  'item_caption_conflict' diagnostic with the input preserved verbatim.
  Intake MUST normalize media captions into the ordered logical body and
  clear item captions BEFORE planning — no implicit drop, no duplication.
  The only in-planner normalization is the unambiguous one: a media-only
  single item whose own caption can no longer share the caption budget with
  the mandatory signature publishes as a signature-only media part plus the
  WHOLE caption verbatim in the next text part — one paragraph, never
  auto-split, never clipped;
- the album caption + per-item-caption restriction is the domain's OWN rule
  (domain.content.Content rejects the combination as ambiguous). The Bot API
  payload itself allows it — owner-facing texts must state this accurately
  and never claim Telegram rejects the combination.

This module imports nothing outside the standard library and domain.content;
it must stay importable offline with no transport, DB or asyncio present.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from .content import (
    ALBUM_MAX,
    CAPTION_MAX,
    TEXT_MAX,
    Content,
    Entity,
    MediaRef,
    _validate_entities,
    utf16_length,
)
from .errors import ContentValidationError
from .signature import SIG_JOIN, SignatureConfig

PLAN_VERSION = 3  # v3: one blank paragraph before the mandatory signature
LEGACY_PLAN_VERSION = 2  # frozen v2 snapshots retain their single-newline join
LOGICAL_VERSION = 2  # logical input shape is unchanged by footer formatting
PART_LABEL = "Часть"
FOOTER_SEP = " | "
FOOTER_JOIN = "\n\n"

# diagnostic codes (stable machine keys)
MANUAL_SPLIT_REQUIRED = "manual_split_required"
ALBUM_OVERFLOW = "album_overflow"
FOOTER_TOO_LARGE = "footer_too_large"
SIGNATURE_CONFLICT = "signature_conflict"
SIGNATURE_LIMIT = "signature_limit_exceeded"
ITEM_CAPTION_CONFLICT = "item_caption_conflict"

_TRAILING_BLANKS = re.compile(r"(?:[ \t]*\n)*[ \t]*\Z")


def footer_text(part_count: int) -> str:
    """Rendered nav footer for a plan of ``part_count`` parts; '' for one."""
    if part_count <= 1:
        return ""
    return FOOTER_SEP.join(f"{PART_LABEL} {i}" for i in range(1, part_count + 1))


def footer_length(part_count: int) -> int:
    """Rendered length in characters (code points), same budget unit as the
    existing domain content limits; URLs live in entities and do not count."""
    return len(footer_text(part_count))


def _signature_join(version: int) -> str:
    if type(version) is not int or version not in (LEGACY_PLAN_VERSION, PLAN_VERSION):
        raise ContentValidationError("publication_plan has an unsupported version")
    return "\n" if version == LEGACY_PLAN_VERSION else SIG_JOIN


def total_footer_length(
    signature: SignatureConfig, part_count: int, *, version: int = PLAN_VERSION,
) -> int:
    """Budget cost of the COMPLETE footer of a part in an N-part plan: joins,
    navigation (multi-part only) and the mandatory signature. Joins are
    counted as if a body sat above them — empty parts publish strictly less."""
    base = footer_length(part_count) + len(FOOTER_JOIN) if part_count > 1 else 0
    return base + len(_signature_join(version)) + signature.length()


@dataclass(frozen=True)
class PlanDiagnostic:
    """Machine code + owner-presentable message + bounded numeric details.

    ``details`` is treated as read-only. Messages are Russian because these
    diagnostics reach the owner; the manual-split wording carries the
    confirmed phrase 'разбить' so the flow can reuse it verbatim.
    """

    code: str
    message: str
    details: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message,
                "details": dict(sorted(self.details.items()))}

    @staticmethod
    def from_json(data: dict[str, Any]) -> "PlanDiagnostic":
        details = data.get("details", {})
        if not isinstance(details, dict) or any(
            not isinstance(k, str) or not isinstance(v, int) or isinstance(v, bool)
            for k, v in details.items()
        ):
            raise ContentValidationError("diagnostic.details must be an int map")
        return PlanDiagnostic(
            code=_require_str(data.get("code"), "diagnostic.code"),
            message=_require_str(data.get("message"), "diagnostic.message"),
            details={k: v for k, v in sorted(details.items())},
        )


def _require_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContentValidationError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class NavSlot:
    """Where the link to ``part_index`` belongs in this part's footer.

    Offsets/length are UTF-16 code units inside the part's final published
    text. ``linked=False`` marks the current part's own label: it stays plain
    text and never receives a link entity (a part never links to itself).
    """

    part_index: int
    utf16_offset: int
    utf16_length: int
    label: str
    linked: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "part_index": self.part_index,
            "utf16_offset": self.utf16_offset,
            "utf16_length": self.utf16_length,
            "label": self.label,
            "linked": self.linked,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> "NavSlot":
        return NavSlot(
            part_index=_require_int(data.get("part_index"), "slot.part_index"),
            utf16_offset=_require_int(data.get("utf16_offset"), "slot.utf16_offset"),
            utf16_length=_require_int(data.get("utf16_length"), "slot.utf16_length"),
            label=_require_str(data.get("label"), "slot.label"),
            linked=data.get("linked") is True,
        )


@dataclass(frozen=True)
class FooterPlan:
    """The rendered footer plus per-label link slots (URLs filled in by the
    post-publish navigation stage)."""

    text: str
    slots: tuple[NavSlot, ...]

    def to_json(self) -> dict[str, Any]:
        return {"text": self.text, "slots": [s.to_json() for s in self.slots]}

    @staticmethod
    def from_json(data: dict[str, Any]) -> "FooterPlan":
        slots = data.get("slots", [])
        if not isinstance(slots, list):
            raise ContentValidationError("footer.slots must be a list")
        return FooterPlan(
            text=_require_str(data.get("text"), "footer.text"),
            slots=tuple(NavSlot.from_json(s) for s in slots if isinstance(s, dict)),
        )


def _require_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContentValidationError(f"{field_name} must be an integer")
    return value


@dataclass(frozen=True)
class LogicalPost:
    """Internal shape of one post while it is being composed: the aggregated
    body (already concatenated text chunks, verbatim, may exceed 4096) and
    the collected media in arrival order (0 = none, 1 = single, 2+ = album).

    This type does NOT relax single-``Content`` validation: every physical
    part produced from it is an ordinary validated ``Content``. Forwards are
    not modelled here — a forward stays one physical publication sent as-is,
    which means the mandatory signature CANNOT be appended to a forwarded
    source (Bot API forwards carry no editable caption). The planner does not
    silently convert forwards; deciding between an editable copy and rejecting
    the forward is an owner-facing choice deferred to the composition UI.

    Media caption contract (CHUNK1b correction): intake MUST normalize an
    item's own caption into the ordered aggregated body and CLEAR the item
    caption before building a LogicalPost. This shape records no arrival
    order between the body and item captions, so a LogicalPost carrying BOTH
    a body and captioned items is ambiguous by construction — plan_post
    answers it with a blocking 'item_caption_conflict' diagnostic and
    preserves the input verbatim; nothing is merged, dropped or duplicated.
    """

    body: str | None = None
    entities: tuple[Entity, ...] = ()
    media: tuple[MediaRef, ...] = ()

    def __post_init__(self) -> None:
        if self.body is not None and not self.body:
            raise ContentValidationError("logical body is empty; use None")
        if self.body is None and not self.media:
            raise ContentValidationError("logical post needs a body or media")
        if self.body is None and self.entities:
            raise ContentValidationError("entities require a body to attach to")
        if len(self.media) > 1:
            for i, item in enumerate(self.media):
                if item.kind not in ("photo", "video"):
                    raise ContentValidationError(
                        f"album item[{i}] must be photo or video, got {item.kind!r}"
                    )
        if self.body is not None:
            _validate_entities(self.body, self.entities, "logical body")

    # ---- versioned JSON snapshot (durable receipts live in CHUNK2) --------
    def to_json(self) -> str:
        data: dict[str, Any] = {"v": LOGICAL_VERSION}
        if self.body is not None:
            data["body"] = self.body
            data["entities"] = [e.to_json() for e in self.entities]
        if self.media:
            data["media"] = [m.to_json() for m in self.media]
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def from_json(raw: str) -> "LogicalPost":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise ContentValidationError("logical_json is not valid JSON") from None
        if not isinstance(data, dict):
            raise ContentValidationError("logical_json must be a JSON object")
        if type(data.get("v")) is not int or data.get("v") != LOGICAL_VERSION:
            raise ContentValidationError("logical_json has an unsupported version")
        ents = data.get("entities", [])
        media = data.get("media", [])
        if not isinstance(ents, list) or not isinstance(media, list):
            raise ContentValidationError("logical_json lists are malformed")
        return LogicalPost(
            body=data["body"] if isinstance(data.get("body"), str) else None,
            entities=tuple(Entity.from_json(e) for e in ents if isinstance(e, dict)),
            media=tuple(MediaRef.from_json(m) for m in media if isinstance(m, dict)),
        )


@dataclass(frozen=True)
class PlannedPart:
    """One physical message of the publication stack, fully validated as an
    ordinary ``Content`` (limits enforced at construction).

    ``body_slice`` is the verbatim contiguous slice of the logical body this
    part carries (possibly ''); joining the slices of all parts in order
    reconstructs the owner's body exactly. The published text omits only the
    structural blank runs documented at module level.
    """

    index: int  # 1-based position in the publication stack
    content: Content
    body_slice: str
    footer: FooterPlan | None = None

    def published_text(self) -> str | None:
        """EXACT text/caption the transport will send for this part — preview
        builders must render from this, never re-derive it."""
        if self.content.kind == "text":
            return self.content.text
        if self.content.kind == "album":
            return self.content.caption
        return self.content.media.caption if self.content.media else None

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "body_slice": self.body_slice,
            "content_json": self.content.to_json(),
            "footer": self.footer.to_json() if self.footer else None,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> "PlannedPart":
        footer_data = data.get("footer")
        if footer_data is not None and not isinstance(footer_data, dict):
            raise ContentValidationError("part.footer must be an object or null")
        return PlannedPart(
            index=_require_int(data.get("index"), "part.index"),
            content=Content.from_json(_require_str(
                data.get("content_json"), "part.content_json")),
            body_slice=data["body_slice"] if isinstance(
                data.get("body_slice"), str) else "",
            footer=FooterPlan.from_json(footer_data)
            if isinstance(footer_data, dict) else None,
        )


@dataclass(frozen=True)
class PublicationPlan:
    """The frozen publication stack for one logical post: parts in physical
    send order (media first when present), plus non-blocking warnings.

    Blocking outcomes are NOT here — they are diagnostics on ``PlanResult``
    with ``plan=None``; a plan that exists is publishable as-is.
    """

    parts: tuple[PlannedPart, ...]
    signature: SignatureConfig
    diagnostics: tuple[PlanDiagnostic, ...] = ()
    version: int = PLAN_VERSION

    def __post_init__(self) -> None:
        sig_join = _signature_join(self.version)
        if not isinstance(self.signature, SignatureConfig):
            raise ContentValidationError(
                "publication_plan requires the embedded signature")
        n = len(self.parts)
        if n == 0:
            raise ContentValidationError("publication plan needs parts")
        sig_text = self.signature.rendered()
        sig_u16 = utf16_length(sig_text)
        for part in self.parts:
            published = part.published_text()
            if published is None:
                raise ContentValidationError(
                    "every part must carry at least the signature")
            if published != sig_text and not published.endswith(sig_join + sig_text):
                raise ContentValidationError(
                    "every part's published text must end with the mandatory "
                    "signature line")
            ents = _part_entities(part)
            if len(ents) < 2 or ents[-2:] != self.signature.link_entities(
                utf16_length(published) - sig_u16
            ):
                raise ContentValidationError(
                    "every part must carry the signature's two link entities "
                    "at the end of its entity list")
        for i, part in enumerate(self.parts, start=1):
            if part.index != i:
                raise ContentValidationError(
                    f"part indices must be contiguous from 1, got {part.index} at {i}"
                )
        f_text = footer_text(n)
        for part in self.parts:
            if n == 1:
                if part.footer is not None:
                    raise ContentValidationError(
                        "single-part plan must not carry a navigation footer")
                continue
            if part.footer is None or part.footer.text != f_text:
                raise ContentValidationError(
                    "multi-part plan needs the exact final-N footer on every part")
            if len(part.footer.slots) != n:
                raise ContentValidationError("footer must have one slot per part")
            plain = [s for s in part.footer.slots if not s.linked]
            if len(plain) != 1 or plain[0].part_index != part.index:
                raise ContentValidationError(
                    "only the current part's own label may stay plain text")
            published = part.published_text()
            if published is None:
                raise ContentValidationError(
                    "multi-part media part must carry at least the footer caption")
            nav_start = len(published) - len(f_text + sig_join + sig_text)
            if (nav_start < 0
                    or published[nav_start:] != f_text + sig_join + sig_text
                    or (nav_start and not published[:nav_start].endswith(FOOTER_JOIN))):
                raise ContentValidationError(
                    "navigation and signature must match the plan version's footer")
            expected_footer = _footer_plan(published, nav_start, part.index, n)
            if part.footer != expected_footer:
                raise ContentValidationError("footer slots must match the final navigation")
            total_u16 = utf16_length(published)
            for slot in part.footer.slots:
                end = slot.utf16_offset + slot.utf16_length
                if slot.utf16_offset < 0 or slot.utf16_length < 1 or end > total_u16:
                    raise ContentValidationError(
                        f"footer slot for part {slot.part_index} is outside the "
                        "published text UTF-16 range")
                if published[
                    _u16_to_char(published, slot.utf16_offset):
                    _u16_to_char(published, end)
                ] != slot.label:
                    raise ContentValidationError(
                        f"footer slot label {slot.label!r} does not match the "
                        "published text at its UTF-16 span")

    def reconstructed_body(self) -> str:
        return "".join(part.body_slice for part in self.parts)

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": self.version,
                "parts": [p.to_json() for p in self.parts],
                "signature": self.signature.to_json_dict(),
                "diagnostics": [d.to_json() for d in self.diagnostics],
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def from_json(raw: str) -> "PublicationPlan":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise ContentValidationError(
                "publication_plan is not valid JSON") from None
        if not isinstance(data, dict):
            raise ContentValidationError("publication_plan must be a JSON object")
        _signature_join(data.get("v"))
        parts = data.get("parts")
        diags = data.get("diagnostics", [])
        if not isinstance(parts, list) or not parts or not isinstance(diags, list):
            raise ContentValidationError("publication_plan lists are malformed")
        return PublicationPlan(
            parts=tuple(PlannedPart.from_json(p)
                        for p in parts if isinstance(p, dict)),
            signature=SignatureConfig.from_json_dict(data.get("signature")),
            diagnostics=tuple(PlanDiagnostic.from_json(d)
                              for d in diags if isinstance(d, dict)),
            version=data["v"],
        )


@dataclass(frozen=True)
class PlanResult:
    """Either a publishable plan (with warning diagnostics on it), or no plan
    plus blocking diagnostics. Never a silently truncated half-plan."""

    plan: PublicationPlan | None
    diagnostics: tuple[PlanDiagnostic, ...] = ()


def _u16_to_char(text: str, utf16_offset: int) -> int:
    """Character index for a UTF-16 code-unit offset (must be on a boundary)."""
    units = 0
    for i, ch in enumerate(text):
        if units == utf16_offset:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > utf16_offset:
            raise ContentValidationError(
                "UTF-16 offset splits a character")
    if units == utf16_offset:
        return len(text)
    raise ContentValidationError("UTF-16 offset past the text end")


# --------------------------------------------------------------------------
# paragraph scanning and entity-safe block construction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Block:
    """Atomic packing unit: a contiguous body span [start, end) that may not
    be split (a paragraph, or paragraphs locked together by an entity that
    crosses the blank-line seam between them)."""

    start: int
    end: int
    trailing: int   # structural blank run closing this block (0 if none)
    strip_blocked: bool  # an entity spans that run -> keep it when publishing

    @property
    def span(self) -> int:
        return self.end - self.start


def _scan(body: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Maximal runs: paragraphs (content characters; line breaks INSIDE a
    paragraph are part of it) and separator runs. A paragraph's own
    terminating newline belongs to the following separator run, so stripping
    a trailing separator at a part seam leaves no dangling newline, while a
    separator published mid-part reproduces the original blank lines exactly.
    Paragraphs and separators tile the body exactly."""
    paras: list[tuple[int, int]] = []
    seps: list[tuple[int, int]] = []
    pos = 0
    n = len(body)
    while pos < n:
        start = pos
        while pos < n:
            nl = body.find("\n", pos)
            eol = nl + 1 if nl != -1 else n
            if body[pos:eol].strip():
                break
            pos = eol
        if pos > start:
            seps.append((start, pos))
        start = pos
        end: int | None = None
        while pos < n:
            nl = body.find("\n", pos)
            eol = nl + 1 if nl != -1 else n
            if not body[pos:eol].strip():
                break
            end = nl if nl != -1 else eol  # content stops before the \\n
            pos = eol
        if end is not None:
            paras.append((start, end))
            pos = end  # the terminating newline joins the separator run
    return paras, seps


def _intersects(entities: tuple[Entity, ...], body: str, start: int, end: int) -> bool:
    """True when any entity spans a UTF-16 unit of body[start:end].

    Ranges passed here are blank-only runs (space/tab/newline: one UTF-16
    unit per character), so the UTF-16 span shifts by exactly the char count.
    """
    if start >= end:
        return False
    u_start = utf16_length(body[:start])
    u_end = u_start + (end - start)
    return any(
        ent.offset < u_end and ent.offset + ent.length > u_start
        for ent in entities
    )


def _blocks(body: str, entities: tuple[Entity, ...]) -> list[_Block]:
    """Split the body into entity-safe blocks. A blank-line separator becomes
    a block boundary only when no entity touches it; otherwise the adjacent
    paragraphs lock into one block, so no entity can ever be cut."""
    n = len(body)
    if n == 0:
        return []
    paras, seps = _scan(body)
    if not paras:
        return [_Block(0, n, 0, False)]  # whitespace-only body: never split
    lead = 1 if (seps and seps[0][0] == 0) else 0
    blocks: list[_Block] = []
    cur = 0
    for i in range(len(paras) - 1):
        sep_start, sep_end = seps[i + lead]
        if not _intersects(entities, body, sep_start, sep_end):
            blocks.append(_Block(cur, sep_end, sep_end - sep_start, False))
            cur = sep_end
    trailing, blocked = 0, False
    if len(seps) > len(paras) - 1 + lead:  # trailing blank run after last para
        trailing_start, trailing_end = seps[-1]
        trailing = trailing_end - trailing_start
        blocked = _intersects(entities, body, trailing_start, trailing_end)
    blocks.append(_Block(cur, n, trailing, blocked))
    return blocks


# --------------------------------------------------------------------------
# budgets, greedy packing, footer convergence
# --------------------------------------------------------------------------


def _diag_footer_too_large(parts: int, footer_len: int) -> PlanDiagnostic:
    return PlanDiagnostic(
        FOOTER_TOO_LARGE,
        f"Навигация на {parts} частей и обязательная подпись занимают "
        f"{footer_len} символов и не оставляют места в части (лимит {TEXT_MAX}); "
        "разбейте содержимое вручную — "
        "план не угадывает число частей и ничего не отбрасывает.",
        {"parts": parts, "footer_length": footer_len},
    )


def _diag_manual_split(length: int, budget: int) -> PlanDiagnostic:
    return PlanDiagnostic(
        MANUAL_SPLIT_REQUIRED,
        f"Абзац на {length} символов не проходит даже с минимальным футером "
        f"(навигация и подпись): в часть помещается {budget} — попроси разбить "
        "текст вручную. "
        "Фрагменты не создаются, содержимое сохранено целиком.",
        {"length": length, "budget": budget},
    )


def _diag_signature_conflict(items: int) -> PlanDiagnostic:
    # Domain-accurate wording: the Bot API accepts an album caption together
    # with per-item captions; domain.content.Content rejects the combination
    # as ambiguous (and the mandatory signature rides in the album caption).
    # Never claim Telegram rejects it — the restriction is the bot's own rule.
    return PlanDiagnostic(
        SIGNATURE_CONFLICT,
        f"Обязательная подпись публикуется в подписи альбома, а у элементов "
        f"({items}) уже есть собственные подписи. Bot API такую комбинацию "
        "допускает, но бот не выбирает за автора, какой текст показывать: "
        "правило бота отклоняет неоднозначную комбинацию. Удали подписи "
        "элементов — обязательная подпись сохраняется. Фрагменты не создаются, "
        "содержимое сохранено.",
        {"items": items},
    )


def _diag_item_caption_conflict(items: int, single: bool) -> PlanDiagnostic:
    """Blocking: an aggregated body AND media items with own captions. The
    intended merge order is unknowable from a LogicalPost, so the planner
    refuses instead of guessing — no overwrite, no silent drop, no exception.
    """
    where = "у медиа" if single else f"у элементов альбома ({items})"
    return PlanDiagnostic(
        ITEM_CAPTION_CONFLICT,
        f"В тексте поста и {where} есть собственный текст: порядок их "
        "объединения планировщику неизвестен, поэтому план не построен и "
        "ничего не опубликовано — содержимое сохранено целиком. Собери "
        "подписи медиа в текст по порядку и очисти подписи элементов до "
        "планирования; планировщик не объединяет их молча и не дублирует.",
        {"items": items},
    )


def _diag_signature_limit(caption_length: int, signature_length: int,
                          limit: int) -> PlanDiagnostic:
    # Defense in depth: with validated MediaRef (caption <= 1024) and capped
    # signature fields the lossless move always fits a text part; this branch
    # only fires if those caps ever change, and still refuses to cut.
    return PlanDiagnostic(
        SIGNATURE_LIMIT,
        f"Подпись элемента ({caption_length} символов) вместе с обязательной "
        f"подписью ({signature_length} символов) и навигацией не помещается "
        f"даже в отдельную текстовую часть (лимит {limit}) — перенести её "
        "целиком некуда: сократи подпись элемента. Подписи не разбиваются, "
        "содержимое сохранено.",
        {"caption_length": caption_length, "signature_length": signature_length,
         "limit": limit},
    )


def _text_groups(
    blocks: list[_Block] | tuple[_Block, ...], budget: int, strip_allowed: bool
) -> tuple[tuple[_Block, ...], ...] | PlanDiagnostic:
    """Greedy contiguous packing: every part takes the MAXIMUM number of
    whole blocks that fit ``budget`` rendered characters. A block that cannot
    fit alone yields the manual-split diagnostic — never a fragment."""
    groups: list[tuple[_Block, ...]] = []
    cur: list[_Block] = []
    raw = 0  # verbatim characters of all blocks in the current part
    for block in blocks:
        tail = block.trailing if (strip_allowed and not block.strip_blocked) else 0
        if not cur:
            if block.span - tail > budget:
                return _diag_manual_split(block.span - tail, budget)
            cur, raw = [block], block.span
        elif raw + block.span - tail <= budget:
            cur.append(block)
            raw += block.span
        else:
            groups.append(tuple(cur))
            if block.span - tail > budget:
                return _diag_manual_split(block.span - tail, budget)
            cur, raw = [block], block.span
    if cur:
        groups.append(tuple(cur))
    return tuple(groups)


_INFEASIBLE_N1 = "infeasible"  # media: whole body does not fit the bare caption


def _structure(
    blocks: list[_Block], body: str, has_media: bool, parts_n: int,
    signature: SignatureConfig, version: int,
):
    """Compute the exact part structure at the candidate part count:
    (caption_block_count, text_groups) | diagnostic | _INFEASIBLE_N1.

    Budgets use the EXACT total footer of ``parts_n`` (navigation when
    multi-part + the mandatory signature + their joins; which may differ from
    ``parts_n`` — the caller iterates to the fixed point)."""
    strip_allowed = parts_n > 1 or version == PLAN_VERSION
    text_budget = TEXT_MAX - total_footer_length(signature, parts_n, version=version)
    if blocks and text_budget < 1:
        return _diag_footer_too_large(parts_n, footer_length(parts_n))
    if has_media:
        cap_budget = CAPTION_MAX - total_footer_length(signature, parts_n, version=version)
        if parts_n > 1 and cap_budget < 0:
            return _diag_footer_too_large(parts_n, footer_length(parts_n))
        if parts_n == 1:
            # one part total: the WHOLE body must fit the caption budget
            # Structural trailing blanks are omitted in v3, but kept in slices.
            tail = _tail(blocks[-1] if blocks else None, strip_allowed)
            if len(body) - tail <= cap_budget:
                return (len(blocks), ())
            return _INFEASIBLE_N1
        caption_count = 0
        used_cap = 0
        for block in blocks:
            tail = 0 if block.strip_blocked else block.trailing
            if used_cap == 0:
                if block.span - tail > cap_budget:
                    break  # first paragraph misses the caption: empty caption
                used_cap = block.span
            elif used_cap + block.span - tail <= cap_budget:
                used_cap += block.span
            else:
                break
            caption_count += 1
        groups = _text_groups(blocks[caption_count:], text_budget, strip_allowed)
        if isinstance(groups, PlanDiagnostic):
            return groups
        return (caption_count, groups)
    groups = _text_groups(blocks, text_budget, strip_allowed)
    if isinstance(groups, PlanDiagnostic):
        return groups
    return (0, groups)


def _count_parts(structure, has_media: bool) -> int:
    _, groups = structure
    return (1 if has_media else 0) + len(groups)


# --------------------------------------------------------------------------
# building validated parts at the converged part count
# --------------------------------------------------------------------------


def _map_entities(
    entities: tuple[Entity, ...], body: str, start: int, end: int
) -> tuple[Entity, ...]:
    """Shift entities that live inside body[start:end] into the part's own
    UTF-16 coordinate space. Blocks are entity-safe by construction, so no
    entity can straddle a slice boundary; a straddle here is an invariant
    violation and fails loudly rather than corrupting offsets."""
    u_start = utf16_length(body[:start])
    u_len = utf16_length(body[start:end])
    mapped: list[Entity] = []
    for ent in entities:
        inside = u_start <= ent.offset and ent.offset + ent.length <= u_start + u_len
        straddles = (
            ent.offset < u_start + u_len and ent.offset + ent.length > u_start
        )
        if straddles and not inside:
            raise ContentValidationError(
                "planner invariant violated: an entity straddles a part boundary"
            )
        if inside:
            mapped.append(replace(ent, offset=ent.offset - u_start))
    return tuple(mapped)


def _footer_plan(published: str, footer_char: int, own_index: int, n: int) -> FooterPlan:
    """Nav slots positioned in the part's FINAL published text (UTF-16 units).
    The own label stays plain (linked=False): a part never carries a link to
    itself; sibling labels are link slots whose URLs arrive with receipts."""
    ftext = footer_text(n)
    base_u16 = utf16_length(published[:footer_char])
    slots: list[NavSlot] = []
    char_off = 0
    for i in range(1, n + 1):
        label = f"{PART_LABEL} {i}"
        slots.append(
            NavSlot(
                part_index=i,
                utf16_offset=base_u16 + utf16_length(ftext[:char_off]),
                utf16_length=utf16_length(label),
                label=label,
                linked=(i != own_index),
            )
        )
        char_off += len(label) + len(FOOTER_SEP)
    return FooterPlan(text=ftext, slots=tuple(slots))


def _assemble(
    pub: str, ftext: str, sig_text: str, use_footer: bool, version: int,
) -> tuple[str, int]:
    """Render a part's FINAL published text: body, blank line, navigation
    (multi-part only), versioned join, mandatory signature. Returns the full
    text and the character offset where the navigation footer starts
    (-1 = no footer)."""
    sig_join = _signature_join(version)
    if use_footer:
        nav_char = len(pub) + len(FOOTER_JOIN) if pub else 0
        head = (pub + FOOTER_JOIN if pub else "") + ftext + sig_join
    else:
        nav_char = -1
        head = pub + sig_join if pub else ""
    return head + sig_text, nav_char


def _part_entities(part: PlannedPart) -> tuple[Entity, ...]:
    """Every entity the transport will send for this part, in order."""
    if part.content.kind == "text":
        return part.content.entities
    if part.content.kind == "album":
        return part.content.caption_entities
    if part.content.media is not None:
        return part.content.media.caption_entities
    return ()


def _tail(block: _Block | None, use_footer: bool) -> int:
    """Chars dropped from the published tail (kept in the snapshot slice):
    only the structural blank run closing the part, and only when no entity
    spans it."""
    if block is None or not use_footer or block.strip_blocked:
        return 0
    return block.trailing


def _strip_caption_tail(caption: str, entities: tuple[Entity, ...]) -> str:
    """Drop only unformatted structural blanks; never move/cut an entity."""
    blocks = _blocks(caption, entities)
    tail = _tail(blocks[-1] if blocks else None, True)
    return caption[:len(caption) - tail]


def _build_parts(
    structure, blocks: list[_Block], logical: LogicalPost,
    used: tuple[MediaRef, ...], n: int, signature: SignatureConfig, version: int,
) -> tuple[PlannedPart, ...]:
    caption_count, groups = structure
    use_footer = n > 1
    strip_allowed = use_footer or version == PLAN_VERSION
    ftext = footer_text(n)
    sig_text = signature.rendered()
    sig_u16 = utf16_length(sig_text)
    body = logical.body or ""
    entities = logical.entities
    parts: list[PlannedPart] = []
    index = 1
    if used:
        if body and any(item.caption is not None for item in used):
            # unreachable via plan_post (blocked as 'item_caption_conflict'
            # first): building anyway would let the body slice silently
            # overwrite the items' own captions — accepted content lost.
            raise ContentValidationError(
                "planner invariant violated: body + item captions must be "
                "rejected as ambiguous before any part is built"
            )
        cap_last = blocks[caption_count - 1] if caption_count else None
        cap_slice = body[:cap_last.end] if cap_last is not None else ""
        cap_pub = cap_slice[: len(cap_slice) - _tail(cap_last, strip_allowed)]
        if body:
            cap_entities = _map_entities(entities, body, 0, len(cap_slice))
        elif len(used) == 1:
            # no logical body: the item's own caption rides verbatim above
            # the mandatory signature
            cap_pub = used[0].caption or ""
            cap_entities = used[0].caption_entities
            if version == PLAN_VERSION:
                cap_pub = _strip_caption_tail(cap_pub, cap_entities)
        else:
            cap_entities = ()
        full, nav_char = _assemble(cap_pub, ftext, sig_text, use_footer, version)
        sig_links = signature.link_entities(utf16_length(full) - sig_u16)
        if len(used) == 1:
            item = used[0]
            media = MediaRef(
                kind=item.kind, file_id=item.file_id,
                caption=full, caption_entities=cap_entities + sig_links,
            )
            content = Content(kind=item.kind, media=media)
        else:
            content = Content(
                kind="album", items=used,
                caption=full, caption_entities=cap_entities + sig_links,
            )
        footer = (
            _footer_plan(full, nav_char, index, n) if use_footer else None
        )
        parts.append(PlannedPart(index=index, content=content,
                                 body_slice=cap_slice, footer=footer))
        index += 1
    for group in groups:
        start, end = group[0].start, group[-1].end
        part_slice = body[start:end]
        pub = part_slice[: len(part_slice) - _tail(group[-1], strip_allowed)]
        full, nav_char = _assemble(pub, ftext, sig_text, use_footer, version)
        content = Content(
            kind="text", text=full,
            entities=_map_entities(entities, body, start, end)
            + signature.link_entities(utf16_length(full) - sig_u16),
        )
        footer = _footer_plan(full, nav_char, index, n) if use_footer else None
        parts.append(PlannedPart(index=index, content=content,
                                 body_slice=part_slice, footer=footer))
        index += 1
    return tuple(parts)


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------


def _plan_single_caption_moved(
    item: MediaRef, signature: SignatureConfig,
    warnings: tuple[PlanDiagnostic, ...], version: int,
) -> PlanResult:
    """Lossless move (CHUNK1b correction): a media-only single item whose own
    caption stopped fitting the caption budget once the mandatory signature
    joined it must NOT be rejected while it still fits a text part.

    The item carries exactly ONE own text, so this normalization is
    unambiguous: part 1 publishes the media with the navigation footer and
    the signature only; part 2 carries the WHOLE caption verbatim as a single
    paragraph — never auto-split at its blank lines, never clipped — closed
    by the same footer and signature. The caption heads part 2, so its own
    entities keep their UTF-16 offsets unchanged and the signature links
    trail them. Neither part carries a body slice (there is no logical body).
    If even a dedicated text part could not hold the whole caption with the
    mandatory footer, a bounded blocking diagnostic comes back instead; the
    input is never mutated.
    """
    caption = item.caption or ""
    if version == PLAN_VERSION:
        caption = _strip_caption_tail(caption, item.caption_entities)
    ftext = footer_text(2)
    sig_text = signature.rendered()
    sig_u16 = utf16_length(sig_text)
    media_cap, media_nav = _assemble("", ftext, sig_text, True, version)
    if len(media_cap) > CAPTION_MAX:
        return PlanResult(
            None, warnings + (_diag_footer_too_large(2, footer_length(2)),))
    text_full, text_nav = _assemble(caption, ftext, sig_text, True, version)
    if len(text_full) > TEXT_MAX:
        return PlanResult(
            None,
            warnings + (_diag_signature_limit(len(caption), signature.length(),
                                              TEXT_MAX),),
        )
    media = MediaRef(
        kind=item.kind, file_id=item.file_id, caption=media_cap,
        caption_entities=signature.link_entities(
            utf16_length(media_cap) - sig_u16),
    )
    parts = (
        PlannedPart(
            index=1, content=Content(kind=item.kind, media=media),
            body_slice="", footer=_footer_plan(media_cap, media_nav, 1, 2),
        ),
        PlannedPart(
            index=2, body_slice="",
            content=Content(
                kind="text", text=text_full,
                entities=item.caption_entities
                + signature.link_entities(utf16_length(text_full) - sig_u16),
            ),
            footer=_footer_plan(text_full, text_nav, 2, 2),
        ),
    )
    # entity accounting for the moved caption mirrors plan_post's body check:
    # the media part carries exactly the two signature links; the text part
    # carries the item's own entities verbatim plus its own two links.
    if (len(_part_entities(parts[0])) != 2
            or len(_part_entities(parts[1])) != len(item.caption_entities) + 2):
        raise ContentValidationError(
            "planner invariant violated: caption entities were not preserved "
            "exactly")
    return PlanResult(
        plan=PublicationPlan(parts=parts, signature=signature,
                             diagnostics=warnings, version=version),
        diagnostics=(),
    )


def plan_post(logical: LogicalPost, signature: SignatureConfig) -> PlanResult:
    """Create a new plan using the current footer format, never a legacy one."""
    return plan_post_for_version(logical, signature, version=PLAN_VERSION)


def plan_post_for_version(
    logical: LogicalPost, signature: SignatureConfig, *, version: int,
) -> PlanResult:
    """Deterministic codec validation for a snapshot's explicit format version.

    Storage uses this ONLY to validate saves of existing frozen pairs. New
    compositions use ``plan_post`` and always receive the current version;
    publishing/navigation must use frozen parts, never call this to re-plan.

    The mandatory signature is a REQUIRED argument: there is no plan without
    one and no bypass flag. Blocking outcomes (manual split required / footer
    too large / item caption conflict / signature conflict or limit) come
    back as ``PlanResult(plan=None, diagnostics=...)``; warnings (album
    overflow) travel on the plan itself. The input is never mutated and
    accepted content is never lost: ambiguous body+caption combinations block
    with the input preserved, the one unambiguous caption normalization moves
    the whole caption to a text part (see _plan_single_caption_moved).
    """
    sig_join = _signature_join(version)
    if not isinstance(signature, SignatureConfig):
        raise ContentValidationError(
            "plan_post requires a configured SignatureConfig: the signature "
            "is mandatory for every publication, there is no bypass")
    body = logical.body or ""
    blocks = _blocks(body, logical.entities)
    warnings: list[PlanDiagnostic] = []
    used = logical.media
    if len(logical.media) > ALBUM_MAX:
        received = len(logical.media)
        warnings.append(PlanDiagnostic(
            ALBUM_OVERFLOW,
            f"В альбоме {received} элементов, Telegram принимает "
            f"{ALBUM_MAX // 5}–{ALBUM_MAX}: в план вошли первые {ALBUM_MAX}, "
            f"остальные {received - ALBUM_MAX} сохранены во входе и не опубликованы.",
            {"received": received, "kept": ALBUM_MAX,
             "extra": received - ALBUM_MAX},
        ))
        used = logical.media[:ALBUM_MAX]
    has_media = bool(used)

    # body + captioned media is the un-normalized intake shape: no arrival
    # order between the body and item captions is recorded anywhere in a
    # LogicalPost, so every merge (and the old body-overwrites-caption build)
    # would guess or lose accepted content. Blocking diagnostic, input whole;
    # intake must normalize captions into the ordered body BEFORE planning.
    if logical.body is not None and any(m.caption is not None for m in used):
        return PlanResult(
            None,
            tuple(warnings)
            + (_diag_item_caption_conflict(len(used), len(used) == 1),),
        )

    # signature guards for media-only posts (no logical body): the signature
    # renders in the album/single caption, where the domain Content rule
    # forbids mixing an album-level caption with per-item captions (the Bot
    # API payload allows the combination; the bot refuses to guess which text
    # the owner meant), and a 1024-char caption budget must still hold after
    # the mandatory line is appended.
    if logical.body is None and used:
        if len(used) > 1 and any(m.caption is not None for m in used):
            return PlanResult(
                None, tuple(warnings) + (_diag_signature_conflict(len(used)),))
        if len(used) == 1 and used[0].caption is not None:
            caption = used[0].caption
            if version == PLAN_VERSION:
                caption = _strip_caption_tail(caption, used[0].caption_entities)
            own = len(caption)
            sig_len = signature.length()
            if own + len(sig_join) + sig_len > CAPTION_MAX:
                # the own caption and the mandatory signature no longer share
                # the caption budget: the item has exactly one own text, so
                # the normalization is unambiguous — signature-only media
                # part + the WHOLE caption moved into the next text part.
                return _plan_single_caption_moved(used[0], signature,
                                                  tuple(warnings), version)

    structure = _structure(blocks, body, has_media, 1, signature, version)
    if isinstance(structure, PlanDiagnostic):
        return PlanResult(None, tuple(warnings) + (structure,))
    if structure is not _INFEASIBLE_N1 and _count_parts(structure, has_media) == 1:
        n = 1
    else:
        # deterministic upward convergence to the fixed point N = parts(N).
        # footer_length grows with N, so budgets shrink and the part count is
        # monotone non-decreasing from below: the sequence can only climb to
        # the fixed point. The seen-set + guard below are pure defense in
        # depth; hitting them is a bounded rejection, never a guessed number.
        n = 2
        seen: set[int] = set()
        guard = len(blocks) + 5
        while True:
            structure = _structure(blocks, body, has_media, n, signature, version)
            if isinstance(structure, PlanDiagnostic):
                return PlanResult(None, tuple(warnings) + (structure,))
            count = _count_parts(structure, has_media)
            if count == n:
                break
            if n in seen or guard <= 0:
                return PlanResult(
                    None,
                    tuple(warnings)
                    + (_diag_footer_too_large(n, footer_length(n)),),
                )
            seen.add(n)
            guard -= 1
            n = count

    parts = _build_parts(structure, blocks, logical, used, n, signature, version)
    plan = PublicationPlan(parts=parts, signature=signature,
                           diagnostics=tuple(warnings), version=version)
    if plan.reconstructed_body() != body:
        raise ContentValidationError(
            "planner invariant violated: part slices do not reconstruct the body")
    mapped_total = 0
    for part in parts:
        if logical.body is None and part.index == 1:
            continue  # media-only first part carries item captions, not body
        mapped_total += len(_part_entities(part)) - 2  # minus signature links
    if mapped_total != len(logical.entities):
        raise ContentValidationError(
            "planner invariant violated: entities were not preserved exactly")
    return PlanResult(plan=plan, diagnostics=())
