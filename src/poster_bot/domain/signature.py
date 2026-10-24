"""Mandatory signature footer: '@main | @partner' as the bottom line of every
published message (CHUNK1b, owner requirement).

Design decisions encoded here (approved brief):
- the signature is NOT optional anywhere it is rendered: the planner refuses
  to produce a plan without one; the config layer blocks publication through
  ``poster_bot.config.validate_signature`` before anything is created/sent;
- display labels are plain generic text ('@channel...' by convention, any
  single line accepted): they are rendered as characters and linked ONLY via
  'text_link' entities — never raw HTML interpolation, never clickable
  callbacks from untrusted input;
- URLs ride inside the entities and never count against the message budget
  (Bot API counts characters after entities parsing); they must be absolute
  HTTPS URLs without credentials, whitespace or control characters — http,
  any other protocol and script pseudo-URLs are rejected structurally,
  offline, before publication (t.me itself is always HTTPS);
- explicit caps (labels <= 64 chars, URLs <= 512 chars) keep the mandatory
  footer feasible inside the SMALLEST budget (a 1024-char caption): the
  footer alone can never crowd a part out; absurd configurations fail at
  config load with env-var-named problems, not at send time;
- validation problems are fixed rule texts naming the FIELD only — a bad
  value is never echoed into an error (the config layer adds the env var
  name; values never reach logs, reports or the owner-facing error text).

This module imports nothing outside the standard library and domain.content/
errors; it stays importable offline with no transport, DB or asyncio present.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .content import Entity, utf16_length
from .errors import ContentValidationError

#: separator between the two labels in the rendered footer
SIG_SEP = " | "
#: exactly one empty paragraph directly above the signature line
SIG_JOIN = "\n\n"
#: explicit caps: mandatory footer stays feasible in every Telegram budget
SIG_LABEL_MAX = 64
SIG_URL_MAX = 512
#: Bot API entity type carrying a URL over plain display text
SIG_ENTITY_TYPE = "text_link"

_SIG_FIELDS = ("main_text", "main_url", "partner_text", "partner_url")

_AUTHORITY_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?")


def _has_control_or_space(value: str) -> bool:
    return any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _label_problems(value: str) -> list[str]:
    rules: list[str] = []
    if not value.strip(" "):
        rules.append("must be a non-empty single line")
    elif any((ch.isspace() and ch != " ") or unicodedata.category(ch) == "Cc"
             for ch in value):
        rules.append(
            "must be a single line without tabs, line separators or control characters")
    if len(value) > SIG_LABEL_MAX:
        rules.append(f"must be at most {SIG_LABEL_MAX} characters")
    return rules


def _url_problems(value: str) -> list[str]:
    rules: list[str] = []
    if value.startswith("https://"):
        # HTTPS is the ONLY accepted scheme (owner-approved hardening): the
        # signature links are trusted, owner-visible hrefs, and t.me itself
        # is always HTTPS — a plaintext http link must fail at config load,
        # never ride into the published footer.
        rest = value[len("https://"):]
        authority = rest.split("/", 1)[0]
        if not _AUTHORITY_RE.fullmatch(authority):
            # covers empty host and any userinfo ('user:pass@' contains
            # characters the host grammar never allows)
            rules.append("must include a valid host without credentials")
    else:
        rules.append("must be an absolute https URL")
    if _has_control_or_space(value):
        rules.append("must not contain whitespace or control characters")
    if len(value) > SIG_URL_MAX:
        rules.append(f"must be at most {SIG_URL_MAX} characters")
    return rules


def signature_problems(
    main_text: str, main_url: str, partner_text: str, partner_url: str
) -> list[tuple[str, str]]:
    """(field, fixed rule text) pairs for every invalid signature field.

    Field names only — values never appear in the output, so the config layer
    can prefix each rule with its env var name safely.
    """
    problems: list[tuple[str, str]] = []
    for value, field, checker in (
        (main_text, "main_text", _label_problems),
        (main_url, "main_url", _url_problems),
        (partner_text, "partner_text", _label_problems),
        (partner_url, "partner_url", _url_problems),
    ):
        problems.extend((field, rule) for rule in checker(value))
    return problems


@dataclass(frozen=True)
class SignatureConfig:
    """One validated signature: display labels plus their link URLs.

    All four fields are required — there is no construction path for a partial
    or missing signature (the all-or-none env contract is enforced by the
    config layer; this type cannot be built with fewer values).
    """

    main_text: str
    main_url: str
    partner_text: str
    partner_url: str

    def __post_init__(self) -> None:
        problems = signature_problems(
            self.main_text, self.main_url, self.partner_text, self.partner_url
        )
        if problems:
            field, rule = problems[0]
            raise ContentValidationError(f"signature.{field} {rule}")

    def rendered(self) -> str:
        return self.main_text + SIG_SEP + self.partner_text

    def length(self) -> int:
        """Rendered length in characters — the same budget unit as the domain
        content limits; URLs live in entities and never count here."""
        return len(self.rendered())

    def link_entities(self, offset_base: int = 0) -> tuple[Entity, Entity]:
        """'text_link' entities placing each URL over its label at
        ``offset_base`` (UTF-16 units) inside the final published text.

        Offsets are UTF-16 code units (Bot API convention), so a surrogate
        pair inside either label shifts the partner label correctly.
        """
        main_len = utf16_length(self.main_text)
        sep_len = utf16_length(SIG_SEP)
        return (
            Entity(type=SIG_ENTITY_TYPE, offset=offset_base, length=main_len,
                   url=self.main_url),
            Entity(type=SIG_ENTITY_TYPE, offset=offset_base + main_len + sep_len,
                   length=utf16_length(self.partner_text), url=self.partner_url),
        )

    # ---- embedded in the frozen plan snapshot (never mutated afterwards) ----
    def to_json_dict(self) -> dict[str, Any]:
        return {
            "main_text": self.main_text,
            "main_url": self.main_url,
            "partner_text": self.partner_text,
            "partner_url": self.partner_url,
        }

    @staticmethod
    def from_json_dict(data: Any) -> "SignatureConfig":
        if not isinstance(data, dict):
            raise ContentValidationError("signature must be a JSON object")
        fields: dict[str, str] = {}
        for name in _SIG_FIELDS:
            value = data.get(name)
            if not isinstance(value, str):
                raise ContentValidationError(
                    f"signature.{name} must be a string")
            fields[name] = value
        return SignatureConfig(**fields)
