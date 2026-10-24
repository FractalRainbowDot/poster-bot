from __future__ import annotations

import pytest

from poster_bot.config import (
    ENV_CHANNEL,
    ENV_END,
    ENV_OWNER,
    ENV_SIG_MAIN_TEXT,
    ENV_SIG_MAIN_URL,
    ENV_SIG_PARTNER_TEXT,
    ENV_SIG_PARTNER_URL,
    ENV_START,
    ENV_TOKEN,
    ENV_TZ,
    PLACEHOLDER_TOKEN,
    Config,
    ConfigError,
    load_config,
    validate_for_live,
    validate_signature,
)

SECRET = "secret-token-never-print"

# the four canonical signature fields (CHUNK1b): every published message must
# carry '@main | @partner' at the bottom; values here are example placeholders
SIG_ENVS = {
    ENV_SIG_MAIN_TEXT: "@channel_main",
    ENV_SIG_MAIN_URL: "https://t.me/channel_main",
    ENV_SIG_PARTNER_TEXT: "@channel_partner",
    ENV_SIG_PARTNER_URL: "https://t.me/channel_partner",
}


def _sig_env(**over) -> dict[str, str]:
    env = dict(SIG_ENVS)
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


def _all_messages(exc: ConfigError) -> str:
    return " | ".join(exc.problems)


class TestOffline:
    def test_defaults_offline_ok(self):
        config = load_config({})
        assert config.timezone == "Europe/Moscow"
        # owner requirement: slot hours default to the full day 00..23
        assert config.slot_start_hour == 0 and config.slot_end_hour == 23
        assert config.owner_user_id == 0 and config.channel_id == 0

    def test_env_can_narrow_slot_hours(self):
        config = load_config({ENV_START: "9", ENV_END: "22"})
        assert config.slot_start_hour == 9 and config.slot_end_hour == 22

    def test_structural_errors_name_env_vars_not_values(self):
        with pytest.raises(ConfigError) as exc_info:
            load_config(
                {ENV_TOKEN: SECRET, ENV_OWNER: "not-a-number", ENV_TZ: "Not/AZone"}
            )
        messages = _all_messages(exc_info.value)
        assert ENV_OWNER in messages and ENV_TZ in messages
        assert SECRET not in messages

    def test_hours_bounds_and_order(self):
        with pytest.raises(ConfigError):
            load_config({ENV_START: "25"})
        with pytest.raises(ConfigError):
            load_config({ENV_START: "20", ENV_END: "10"})

    def test_placeholder_token_allowed_offline(self):
        config = load_config({ENV_TOKEN: PLACEHOLDER_TOKEN})
        assert config.bot_token == PLACEHOLDER_TOKEN


class TestChannelTarget:
    """CHANNEL_ID accepts a numeric chat id (existing semantics) or a
    syntactically valid @channel-username — validated offline, values never
    echoed, the username resolution itself is a live-startup step."""

    def test_username_accepted_offline_without_network(self):
        config = load_config({ENV_CHANNEL: "@test_chan_public2"})
        assert config.channel_id == "@test_chan_public2"

    def test_username_passes_live_gate_offline(self):
        # fake values only: the live gate is a pure structure check
        config = load_config(
            {ENV_TOKEN: SECRET, ENV_OWNER: "111", ENV_CHANNEL: "@chan_news1"}
        )
        validate_for_live(config)

    def test_username_case_and_digits_allowed(self):
        config = load_config({ENV_CHANNEL: "@Chan_News2"})
        assert config.channel_id == "@Chan_News2"

    def test_surrounding_whitespace_stripped(self):
        config = load_config({ENV_CHANNEL: "  @chan_news1 \n"})
        assert config.channel_id == "@chan_news1"

    def test_bare_username_requires_at_prefix(self):
        with pytest.raises(ConfigError):
            load_config({ENV_CHANNEL: "chan_news1"})

    @pytest.mark.parametrize("bad", [
        "@",                  # empty username
        "@@chan1",            # doubled marker / injection attempt
        "@ab",                # too short (Telegram: 5..32 chars)
        "@" + "a" * 33,       # too long
        "@5chan1",            # must start with a letter
        "@chan el",           # embedded whitespace
        "@chan\tele",         # embedded tab
        "@chan-el",           # hyphen is not a username character
        "@chan?x=1",          # URL/injection characters
        "@chan%0A",           # percent-encoded injection
        "@https://t.me/chan", # URL disguised as a username
        "https://t.me/chan1", # plain URL target
        "123abc",             # neither numeric nor @username
        "",                   # present but empty
        "@na me; rm -rf /",   # shell/whitespace injection
    ])
    def test_bad_usernames_rejected_with_field_only_wording(self, bad):
        # the problem wording is FIXED text: input-dependent values can never
        # be echoed into it (env var name only, like the token/owner gate)
        if bad.startswith("@"):
            expected = (
                f"{ENV_CHANNEL} must be a valid @channel-username "
                "(5..32 letters, digits and underscores, letter first)"
            )
        else:
            expected = (
                f"{ENV_CHANNEL} must be a numeric channel id or a valid "
                "@channel-username"
            )
        with pytest.raises(ConfigError) as exc_info:
            load_config({ENV_CHANNEL: bad})
        assert _all_messages(exc_info.value) == expected

    def test_numeric_target_semantics_unchanged(self):
        assert load_config({ENV_CHANNEL: "-100555"}).channel_id == -100555
        assert load_config({ENV_CHANNEL: "0"}).channel_id == 0
        # numeric zero still blocks live, the message names the env var
        config = load_config({ENV_TOKEN: SECRET, ENV_OWNER: "1", ENV_CHANNEL: "0"})
        with pytest.raises(ConfigError) as exc_info:
            validate_for_live(config)
        assert ENV_CHANNEL in _all_messages(exc_info.value)


class TestLiveGate:
    def test_dummy_config_blocks_live(self):
        config = load_config({})
        problems = config.live_problems()
        assert len(problems) == 3
        with pytest.raises(ConfigError) as exc_info:
            validate_for_live(config)
        assert ENV_TOKEN in _all_messages(exc_info.value)

    def test_real_values_pass_live(self):
        config = load_config(
            {
                ENV_TOKEN: SECRET,
                ENV_OWNER: "111",
                ENV_CHANNEL: "-100555",
            }
        )
        validate_for_live(config)  # negative channel id (supergroup) is valid

    def test_placeholder_token_rejects_live(self):
        config = load_config(
            {ENV_TOKEN: PLACEHOLDER_TOKEN, ENV_OWNER: "1", ENV_CHANNEL: "-2"}
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_for_live(config)
        assert SECRET not in _all_messages(exc_info.value)

    def test_live_errors_never_quote_values(self):
        config = load_config({ENV_TOKEN: SECRET, ENV_OWNER: "1", ENV_CHANNEL: "0"})
        with pytest.raises(ConfigError) as exc_info:
            validate_for_live(config)
        assert SECRET not in _all_messages(exc_info.value)


class TestTokenNeverInRepr:
    """An accidental ``%s``/f-string/log of a whole Config must not leak the
    bot token: the field is repr-excluded while the value stays usable."""

    def test_repr_and_str_hide_the_bot_token(self):
        config = load_config(
            {
                ENV_TOKEN: SECRET,
                ENV_OWNER: "111",
                ENV_CHANNEL: "-100555",
            }
        )
        assert SECRET not in repr(config)
        assert SECRET not in str(config)
        assert SECRET not in f"{config}"
        assert SECRET not in f"{config!r}"
        # the value itself remains fully usable
        assert config.bot_token == SECRET

    def test_repr_still_shows_the_non_secret_fields(self):
        config = load_config({})
        shown = repr(config)
        assert "Europe/Moscow" in shown
        assert "data/poster_bot.sqlite3" in shown
        assert "bot_token" not in shown  # hidden, not masked with a placeholder


class TestSignatureConfig:
    """Four canonical fields configure the mandatory publication footer.
    All four belong together (all-or-none); every problem message names the
    env var and the fixed rule only — values are never echoed back."""

    def test_raw_compose_quoted_partner_label_passes_startup(self):
        # env_file format: raw retains the quotes in the process environment.
        config = load_config(_sig_env(**{
            ENV_TOKEN: SECRET, ENV_OWNER: "111", ENV_CHANNEL: "-100555",
            ENV_SIG_PARTNER_TEXT: '"@канал партнера"',
        }))
        validate_for_live(config)
        validate_signature(config)
        assert config.signature.partner_text == "@канал партнера"
        assert config.signature.rendered() == "@channel_main | @канал партнера"

    def test_all_four_fields_parse_into_signature(self):
        config = load_config(_sig_env())
        assert config.signature is not None
        assert config.signature.main_text == "@channel_main"
        assert config.signature.main_url == "https://t.me/channel_main"
        assert config.signature.partner_text == "@channel_partner"
        assert config.signature.partner_url == "https://t.me/channel_partner"

    def test_absent_signature_keeps_offline_contracts(self):
        # offline --check must not start failing just because the owner has
        # not filled the new fields yet (live publication is the demanding
        # stage — see validate_signature, wired by the publisher in CHUNK2)
        config = load_config({})
        assert config.signature is None
        assert len(config.live_problems()) == 3  # existing live gate unchanged

    def test_constructor_default_keeps_external_construction_working(self):
        config = Config(
            bot_token="t", owner_user_id=0, channel_id=0, db_path="d",
            lock_path="l", timezone="Europe/Moscow",
            slot_start_hour=0, slot_end_hour=23,
        )
        assert config.signature is None

    @pytest.mark.parametrize("missing", [
        ENV_SIG_MAIN_TEXT, ENV_SIG_MAIN_URL,
        ENV_SIG_PARTNER_TEXT, ENV_SIG_PARTNER_URL,
    ])
    def test_partial_signature_is_all_or_none(self, missing):
        env = _sig_env(**{missing: None})
        with pytest.raises(ConfigError) as exc_info:
            load_config(env)
        messages = _all_messages(exc_info.value)
        assert missing in messages
        assert "all four" in messages

    def test_single_field_alone_names_the_three_missing(self):
        with pytest.raises(ConfigError) as exc_info:
            load_config({ENV_SIG_MAIN_TEXT: "@only_one_channel"})
        messages = _all_messages(exc_info.value)
        for name in (ENV_SIG_MAIN_URL, ENV_SIG_PARTNER_TEXT, ENV_SIG_PARTNER_URL):
            assert name in messages

    def test_whitespace_only_field_counts_as_missing(self):
        with pytest.raises(ConfigError) as exc_info:
            load_config(_sig_env(**{ENV_SIG_PARTNER_URL: "   "}))
        assert ENV_SIG_PARTNER_URL in _all_messages(exc_info.value)

    def test_surrounding_spaces_are_stripped(self):
        config = load_config(_sig_env(**{ENV_SIG_MAIN_TEXT: "  @channel_main  "}))
        assert config.signature.main_text == "@channel_main"

    @pytest.mark.parametrize("field", [ENV_SIG_MAIN_TEXT, ENV_SIG_PARTNER_TEXT])
    @pytest.mark.parametrize("raw, expected", [
        ('"@канал партнера"', "@канал партнера"),
        ("'@канал партнера'", "@канал партнера"),
        ('  "Канал "Партнёр""  ', 'Канал "Партнёр"'),
        (r'"Канал \"Партнёр\""', r'Канал \"Партнёр\"'),
        ('Канал "Партнёр"', 'Канал "Партнёр"'),
        ("Канал O'Брайена", "Канал O'Брайена"),
        ('"Канал', '"Канал'),  # unmatched quotes remain legitimate text
        ('"\'Канал\'"', "'Канал'"),  # remove exactly one pair
    ])
    def test_label_env_normalization_is_narrow(self, field, raw, expected):
        config = load_config(_sig_env(**{field: raw}))
        attr = "main_text" if field == ENV_SIG_MAIN_TEXT else "partner_text"
        assert getattr(config.signature, attr) == expected

    @pytest.mark.parametrize("quote", ["", '"', "'"])
    def test_label_cap_applies_after_quote_normalization(self, quote):
        label = "🌲 канал " + "я" * 56  # 64 visible code points
        config = load_config(_sig_env(**{ENV_SIG_MAIN_TEXT: quote + label + quote}))
        assert config.signature.main_text == label
        with pytest.raises(ConfigError, match="64"):
            load_config(_sig_env(**{ENV_SIG_MAIN_TEXT: quote + label + "я" + quote}))

    @pytest.mark.parametrize("bad", [
        "@канал\n", "\r@канал", "@канал\t", '"@канал\n"',
        "@канал\x85партнера", "@канал\u2028партнера", "@канал\u2029партнера",
        '"   "',
    ])
    def test_label_controls_are_not_hidden_by_normalization(self, bad):
        with pytest.raises(ConfigError) as exc_info:
            load_config(_sig_env(**{ENV_SIG_PARTNER_TEXT: bad}))
        assert ENV_SIG_PARTNER_TEXT in str(exc_info.value)
        assert bad not in str(exc_info.value)

    def test_label_compatibility_does_not_unquote_urls_or_token(self):
        config = load_config(_sig_env(**{ENV_TOKEN: '"dummy-token"'}))
        assert config.bot_token == '"dummy-token"'
        with pytest.raises(ConfigError) as exc_info:
            load_config(_sig_env(**{ENV_SIG_PARTNER_URL: '"https://t.me/partner"'}))
        assert ENV_SIG_PARTNER_URL in str(exc_info.value)

    @pytest.mark.parametrize("bad", [
        "ftp://files.example/doc",
        "javascript:alert(1)",
        "https://",
        "https://user:pw@host.example/ch",
        "https://host.example/a b",
        "https://host.example/a\nb",
        "https://" + "d" * 600,
        "https://MARKERVAL.example/a b",
    ])
    def test_bad_urls_name_the_env_var_and_never_echo_the_value(self, bad):
        env = _sig_env(**{ENV_SIG_PARTNER_URL: bad})
        with pytest.raises(ConfigError) as exc_info:
            load_config(env)
        messages = _all_messages(exc_info.value)
        assert ENV_SIG_PARTNER_URL in messages
        assert "MARKERVAL" not in messages

    @pytest.mark.parametrize("bad", [
        "@chan\nsecond line",
        "@chan\ttabbed",
        "@" + "x" * 70,
        "@MARKERVAL\nsecond line",
    ])
    def test_bad_labels_name_the_env_var_and_never_echo_the_value(self, bad):
        env = _sig_env(**{ENV_SIG_MAIN_TEXT: bad})
        with pytest.raises(ConfigError) as exc_info:
            load_config(env)
        messages = _all_messages(exc_info.value)
        assert ENV_SIG_MAIN_TEXT in messages
        assert "MARKERVAL" not in messages

    def test_plain_http_partner_url_is_refused_named_env(self):
        # HTTPS-only hardening (owner-approved): a plain http signature link
        # is a config error; the report names the env var, never the value.
        env = _sig_env(**{ENV_SIG_PARTNER_URL:
                          "http://MARKERval.example/partner"})
        with pytest.raises(ConfigError) as exc_info:
            load_config(env)
        messages = _all_messages(exc_info.value)
        assert ENV_SIG_PARTNER_URL in messages
        assert "https" in messages  # the rule names the only allowed scheme
        assert "MARKERval" not in messages

    def test_https_partner_url_accepted_verbatim(self):
        config = load_config(_sig_env(**{
            ENV_SIG_PARTNER_URL: "https://t.me/partner"}))
        assert config.signature.partner_url == "https://t.me/partner"

    def test_all_problems_accumulate_in_one_report(self):
        with pytest.raises(ConfigError) as exc_info:
            load_config(_sig_env(**{
                ENV_SIG_MAIN_URL: "ftp://bad.example",
                ENV_SIG_PARTNER_TEXT: "@bad\nlabel",
            }))
        messages = _all_messages(exc_info.value)
        assert ENV_SIG_MAIN_URL in messages and ENV_SIG_PARTNER_TEXT in messages


class TestValidateSignature:
    """Publication-time gate: the publisher must call this BEFORE creating or
    sending anything; a bot without a configured signature cannot publish."""

    def test_missing_signature_blocks_publication(self):
        config = load_config({ENV_TOKEN: SECRET, ENV_OWNER: "1", ENV_CHANNEL: "-2"})
        with pytest.raises(ConfigError) as exc_info:
            validate_signature(config)
        messages = _all_messages(exc_info.value)
        for name in SIG_ENVS:
            assert name in messages

    def test_configured_signature_passes(self):
        validate_signature(load_config(_sig_env()))

    def test_live_startup_gate_still_reports_only_legacy_problems(self):
        # run startup semantics are unchanged by CHUNK1b (existing contract);
        # the signature is enforced at the publication stage, not at run()
        config = load_config({ENV_TOKEN: SECRET, ENV_OWNER: "1", ENV_CHANNEL: "-2"})
        validate_for_live(config)  # no signature fields: still startup-legal
        assert config.signature is None
