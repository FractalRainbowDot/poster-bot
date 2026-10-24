"""Lossless legacy Content -> LogicalPost normalization (shared domain rule).

Lives in the domain (not the UI) because BOTH sides of the planned pipeline
need the exact same lossless fold: the owner preview (ui/flow via ui/intake)
and the gated sender auto-preparing a legacy planless copy before its claim.
One rule, one implementation — preview and delivery can never diverge.

Contract (CHUNK4 correction, approved mandatory-signature upgrade):
- media/album captions become TRAILING body paragraphs in stored media order,
  caption entities remapped onto the merged UTF-16 offsets; item captions are
  cleared (the planner's conflict guard must never fire on legacy data);
- NO strip() anywhere: the joined body stays character-for-character what the
  stored entity offsets were computed against (stripping silently invalidated
  every leading-whitespace entity of a legacy draft); nothing is dropped,
  nothing is duplicated;
- a forward (kind or mode) is NOT normalizable — there is no editable text to
  sign, and copying a forward is an owner decision, never an automatic one.

Pure logic: imports nothing outside the standard library and domain modules;
no I/O, no Telegram, no ambient config.
"""
from __future__ import annotations

from dataclasses import dataclass

from .content import (
    Content,
    ContentValidationError,
    Entity,
    MediaRef,
    utf16_length,
)
from .planner import (
    LogicalPost,
    PublicationPlan,
    PlanDiagnostic,
    plan_post,
)
from .signature import SignatureConfig

PARAGRAPH_SEP = "\n\n"
_SEP_U16 = 2  # two BMP code units

# stable machine codes for preparation outcomes (owner notices and offline
# migration readiness carry ONLY these codes plus ids — never raw content)
PREPARABLE = "preparable"
FORWARD_REVIEW = "forward"
EMPTY_POST = "empty_post"
CONTENT_INVALID = "content_invalid"


def normalize_content(content: Content) -> LogicalPost:
    """Bring a legacy stored Content (media with captions) into planned
    shape: media/album captions become trailing body paragraphs in stored
    media order, caption entities remap onto the merged body. A forward is
    not normalizable — there is no editable text to sign."""
    if content.kind == "forward" or content.mode == "forward":
        raise ContentValidationError(
            "пересланный пост нельзя нормализовать в план: создайте "
            "редактируемую копию")
    parts: list[str] = []
    entities = list(content.entities)
    used = utf16_length(content.text) if content.text else 0
    if content.text:
        parts.append(content.text)

    def fold(caption: str | None, ents: tuple[Entity, ...]) -> None:
        nonlocal used
        if not caption:
            return
        if parts:
            used += _SEP_U16
        parts.append(caption)
        for ent in ents:
            entities.append(Entity(type=ent.type, offset=ent.offset + used,
                                   length=ent.length, url=ent.url,
                                   user_id=ent.user_id, username=ent.username,
                                   custom_emoji_id=ent.custom_emoji_id,
                                   language=ent.language))
        used += utf16_length(caption)

    media: list[MediaRef] = []
    if content.kind == "album":
        fold(content.caption, content.caption_entities)
        for item in content.items:
            fold(item.caption, item.caption_entities)
            media.append(MediaRef(kind=item.kind, file_id=item.file_id))
    elif content.media is not None:
        fold(content.media.caption, content.media.caption_entities)
        media.append(MediaRef(kind=content.media.kind,
                              file_id=content.media.file_id))
    # NO strip(): the joined body must stay character-for-character what the
    # offsets in `entities` were computed against (stripping here silently
    # invalidated every leading-whitespace entity of a legacy draft)
    body = PARAGRAPH_SEP.join(parts) or None
    if body is None and not media:
        raise ContentValidationError("пост пуст")
    return LogicalPost(body=body,
                       entities=tuple(entities),
                       media=tuple(media))


@dataclass(frozen=True)
class Preparation:
    """Pure classification result for preparing one stored legacy Content
    into a frozen pair under a configured signature.

    ``plan``/``logical`` are set exactly when the pair can be persisted
    (``preparable``); otherwise ``codes`` carries the blocking machine
    codes ('forward', 'empty_post', 'content_invalid' or the planner's own
    diagnostic codes). Content never appears in either branch."""

    logical: LogicalPost | None = None
    plan: PublicationPlan | None = None
    diagnostics: tuple[PlanDiagnostic, ...] = ()

    @property
    def preparable(self) -> bool:
        return self.plan is not None

    @property
    def codes(self) -> tuple[str, ...]:
        if self.preparable:
            return (PREPARABLE,)
        return tuple(sorted({d.code for d in self.diagnostics}))


#: diagnostic carrying the not-normalizable machine codes (pure, content-free)
def _code_diagnostic(code: str, message: str) -> PlanDiagnostic:
    return PlanDiagnostic(code=code, message=message)


def prepare_legacy(content: Content, signature: SignatureConfig) -> Preparation:
    """Deterministic, side-effect-free answer to "can this stored legacy
    Content become a frozen signed plan right now, and what is that pair?"

    Runs the SAME pure normalizer the preview uses and the SAME deterministic
    planner the save path re-validates against — if this says 'preparable',
    PlanStore.save accepts the pair. Blocking reasons come back as machine
    codes only (plus the planner's owner-presentable diagnostics); raw user
    content never leaves this function.
    """
    if content.kind == "forward" or content.mode == "forward":
        return Preparation(diagnostics=(_code_diagnostic(
            FORWARD_REVIEW,
            "Пересланный пост нельзя подписать автоматически: создайте"
            " редактируемую копию через предпросмотр."),),)
    if signature is None or not isinstance(signature, SignatureConfig):
        raise ContentValidationError(
            "preparing a plan requires a configured SignatureConfig")
    try:
        logical = normalize_content(content)
    except ContentValidationError as exc:
        # 'пост пуст' is the only empty failure normalize_content raises for
        # non-forward content; anything else is stored-content corruption.
        code = EMPTY_POST if not _has_visible_payload(content) \
            else CONTENT_INVALID
        return Preparation(diagnostics=(_code_diagnostic(
            code, f"Контент нельзя привести к плану: {exc}"),),)
    result = plan_post(logical, signature)
    return Preparation(logical=logical, plan=result.plan,
                       diagnostics=result.diagnostics)


def _has_visible_payload(content: Content) -> bool:
    if content.text:
        return True
    if content.kind == "album":
        return bool(content.items) or bool(content.caption)
    return content.media is not None or bool(content.caption)