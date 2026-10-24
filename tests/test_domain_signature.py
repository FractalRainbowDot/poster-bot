"""Signature footer value type: validation, rendering, UTF-16 link entities.

CHUNK1b contract (approved brief): the mandatory signature '@main | @partner'
is the BOTTOM line of every published message/caption. Display labels are
plain generic text (never raw HTML), links ride as 'text_link' entities only
(never clickable callbacks from untrusted input). Validation problems are
fixed rule texts naming the field only — a bad value can never be echoed
into an error message; the config layer prefixes the env var name.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from poster_bot.domain.content import utf16_length
from poster_bot.domain.errors import ContentValidationError
from poster_bot.domain.signature import (
    SIG_ENTITY_TYPE,
    SIG_JOIN,
    SIG_LABEL_MAX,
    SIG_SEP,
    SIG_URL_MAX,
    SignatureConfig,
    signature_problems,
)

MAIN = "@channel_main"
MAIN_URL = "https://t.me/channel_main"
PARTNER = "@channel_partner"
PARTNER_URL = "https://t.me/channel_partner"
RENDERED = "@channel_main | @channel_partner"  # 13 + 3 + 16, documented literal


def _sig(**over) -> SignatureConfig:
    fields = {"main_text": MAIN, "main_url": MAIN_URL,
              "partner_text": PARTNER, "partner_url": PARTNER_URL}
    fields.update(over)
    return SignatureConfig(**fields)


class TestRendering:
    def test_rendered_footer_and_length(self):
        sig = _sig()
        assert sig.rendered() == RENDERED
        assert sig.length() == 32
        assert SIG_SEP == " | "
        assert SIG_JOIN == "\n\n"  # exactly one empty paragraph above it

    def test_all_four_fields_are_required(self):
        with pytest.raises(TypeError):
            SignatureConfig(MAIN, MAIN_URL, PARTNER)  # no bypass by omission


class TestLinkEntities:
    def test_entities_land_on_labels_in_utf16_space(self):
        sig = _sig()
        first, second = sig.link_entities()
        assert first.type == SIG_ENTITY_TYPE == "text_link"
        assert (first.offset, first.length) == (0, utf16_length(MAIN))
        assert first.url == MAIN_URL
        expected_second = utf16_length(MAIN) + utf16_length(SIG_SEP)
        assert (second.offset, second.length) == (
            expected_second, utf16_length(PARTNER),
        )
        assert second.url == PARTNER_URL

    def test_astral_label_shifts_partner_offset_by_surrogates(self):
        tree = "\N{CHRISTMAS TREE}"  # 1 code point, 2 UTF-16 units
        sig = _sig(main_text=tree + "канал")
        first, second = sig.link_entities()
        assert first.length == utf16_length(tree + "канал")
        assert second.offset == utf16_length(tree + "канал") + utf16_length(SIG_SEP)

    def test_offset_base_places_entities_inside_published_text(self):
        prefix = "текст " + "\N{CHRISTMAS TREE}" * 2 + "\n\nЧасть 1 | Часть 2\n"
        first, second = _sig().link_entities(utf16_length(prefix))
        rendered = prefix + RENDERED
        raw = rendered.encode("utf-16-le")
        assert raw[2 * first.offset : 2 * (first.offset + first.length)] == (
            MAIN.encode("utf-16-le")
        )
        assert raw[2 * second.offset : 2 * (second.offset + second.length)] == (
            PARTNER.encode("utf-16-le")
        )


class TestLabelValidation:
    def test_label_at_max_length_accepted(self):
        _sig(main_text="@" + "k" * (SIG_LABEL_MAX - 1))  # exactly 64

    def test_empty_label_exact_rule(self):
        problems = signature_problems(main_text="", main_url=MAIN_URL,
                                      partner_text=PARTNER, partner_url=PARTNER_URL)
        assert ("main_text", "must be a non-empty single line") in problems

    def test_label_over_max_rejected(self):
        # boundary: SIG_LABEL_MAX + 1 must fail
        with pytest.raises(ContentValidationError) as exc_info:
            _sig(main_text="@" + "k" * SIG_LABEL_MAX)
        assert "main_text" in str(exc_info.value)
        assert "64" in str(exc_info.value)

    @pytest.mark.parametrize("bad", ["@chan\nMain", "@chan\tMain", "@chan\rMain",
                                     "@chan\x00Main"])
    def test_newline_and_control_chars_never_break_the_footer(self, bad):
        problems = signature_problems(main_text=bad, main_url=MAIN_URL,
                                      partner_text=PARTNER, partner_url=PARTNER_URL)
        assert [field for field, _ in problems] == ["main_text"]

    @pytest.mark.parametrize("bad", ["", "@chan\nMain", "@chan\tMain",
                                     "@chan\rMain", "@chan\x00Main",
                                     "@  \t ", "@chan\x85el", "@chan\u2028el",
                                     "@chan\u2029el", "@chan\x7fel", "   "])
    def test_label_must_be_a_nonempty_single_line_without_controls(self, bad):
        problems = signature_problems(main_text=bad, main_url=MAIN_URL,
                                      partner_text=PARTNER, partner_url=PARTNER_URL)
        assert [field for field, _ in problems] == ["main_text"]

    def test_human_labels_preserve_spaces_and_literal_quotes(self):
        sig = _sig(main_text='🌲 Канал "Главный"', partner_text="@канал партнера")
        assert sig.rendered() == '🌲 Канал "Главный" | @канал партнера'


class TestUrlValidation:
    def test_https_urls_accepted_both_fields(self):
        # HTTPS is the ONLY accepted scheme (owner-approved hardening): the
        # t.me canonical links and any https host are valid for both fields.
        _sig(main_url="https://t.me/channel_main",
             partner_url="https://t.me/partner_channel")

    @pytest.mark.parametrize("bad", [
        "http://t.me/partner_channel",     # plain http is refused
        "http://MARKERval.example/p",      # never echoed, refused outright
    ])
    def test_plain_http_is_refused_https_only(self, bad):
        problems = signature_problems(main_text=MAIN, main_url=MAIN_URL,
                                      partner_text=PARTNER, partner_url=bad)
        assert [field for field, _ in problems] == ["partner_url"]
        rule = problems[0][1]
        assert "https" in rule  # the rule names the only allowed scheme
        assert "MARKERval" not in rule and "t.me" not in rule  # safe wording

    @pytest.mark.parametrize("bad", [
        "ftp://files.example/doc",     # arbitrary protocol
        "javascript:alert(1)",         # script pseudo-URL
        "@channel_main",               # not a URL at all
        "channel_main.example/x",      # scheme missing
        "https://",                    # no host
        "https://user:pw@host.example/ch",  # credentials in URL
        "https://user@host.example/ch",     # any userinfo is rejected
        "http://user:pw@host.example/ch",   # userinfo over http: refused too
        "https://host.example/a b",    # embedded space
        "https://host.example/a\nb",   # newline would break the footer line
        "https://host.example/a\tb",   # tab
    ])
    def test_bad_urls_rejected(self, bad):
        problems = signature_problems(main_text=MAIN, main_url=bad,
                                      partner_text=PARTNER, partner_url=PARTNER_URL)
        assert [field for field, _ in problems] == ["main_url"]

    @pytest.mark.parametrize("bad", [
        "https://host.example/a b", "https://host.example/a\nb",
    ])
    def test_injected_newline_and_space_are_still_rejected(self, bad):
        assert signature_problems(main_text=MAIN, main_url=bad,
                                  partner_text=PARTNER, partner_url=PARTNER_URL)

    def test_url_over_max_rejected(self):
        with pytest.raises(ContentValidationError) as exc_info:
            _sig(partner_url="https://host.example/" + "d" * SIG_URL_MAX)
        assert "partner_url" in str(exc_info.value)
        assert "512" in str(exc_info.value)

    def test_long_path_and_query_urls_accepted(self):
        _sig(main_url="https://host.example/" + "p" * 400 + "?q=1&r=2")


class TestSafeErrorWording:
    def test_invalid_values_are_never_echoed(self):
        marker = "SECRETval-имя-токена"
        with pytest.raises(ContentValidationError) as exc_info:
            _sig(main_text=marker + "\nsecond line",
                 main_url=f"https://{marker}.example/x y",
                 partner_text=marker,
                 partner_url="ftp://" + marker)
        message = str(exc_info.value)
        assert marker not in message
        assert "main_text" in message or "main_url" in message


class TestJson:
    def test_stored_labels_are_never_unquoted(self):
        sig = _sig(main_text='"@main"', partner_text="'@partner'")
        data = sig.to_json_dict()
        assert SignatureConfig.from_json_dict(data).to_json_dict() == data
        assert SignatureConfig.from_json_dict(data).rendered() == '\"@main\" | \'@partner\''

    def test_roundtrip_is_lossless(self):
        sig = _sig()
        assert SignatureConfig.from_json_dict(sig.to_json_dict()) == sig

    @pytest.mark.parametrize("missing", ["main_text", "main_url",
                                         "partner_text", "partner_url"])
    def test_missing_field_rejected(self, missing):
        data = _sig().to_json_dict()
        data.pop(missing)
        with pytest.raises(ContentValidationError):
            SignatureConfig.from_json_dict(data)

    def test_bad_url_in_json_rejected(self):
        data = _sig().to_json_dict()
        data["partner_url"] = "javascript:alert(1)"
        with pytest.raises(ContentValidationError):
            SignatureConfig.from_json_dict(data)

    def test_non_string_field_rejected(self):
        data = _sig().to_json_dict()
        data["main_text"] = 5
        with pytest.raises(ContentValidationError):
            SignatureConfig.from_json_dict(data)

    def test_roundtrip_dict_keys_are_exact(self):
        assert sorted(_sig().to_json_dict()) == [
            "main_text", "main_url", "partner_text", "partner_url",
        ]


def test_signature_module_is_domain_pure():
    source = pathlib.Path("src/poster_bot/domain/signature.py").read_text(
        encoding="utf-8")
    allowed_stdlib = {"__future__", "dataclasses", "re", "typing", "unicodedata"}
    allowed_relative = {"content", "errors"}
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed_stdlib, alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if node.level > 0:
                assert mod in allowed_relative, node.module
            else:
                assert mod in allowed_stdlib, node.module
