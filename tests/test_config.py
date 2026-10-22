from __future__ import annotations

import pytest

from poster_bot.config import (
    ENV_CHANNEL,
    ENV_END,
    ENV_OWNER,
    ENV_START,
    ENV_TOKEN,
    ENV_TZ,
    PLACEHOLDER_TOKEN,
    ConfigError,
    load_config,
    validate_for_live,
)

SECRET = "secret-token-never-print"


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