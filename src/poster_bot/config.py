"""Environment-backed configuration.

Errors never echo secret values: problem messages reference env var names only.
Offline mode accepts dummy values; live mode rejects placeholder/missing
token, non-positive owner id, zero channel id.

The channel target is either a numeric chat id (int, existing semantics) or a
public ``@channel-username`` (str). Usernames are validated structurally,
offline, against Telegram's username syntax; resolving one to its stable
numeric id is a live-startup step (see ``poster_bot.live.resolve_channel``)
and never happens here — ``--check`` stays network-free for both forms.

The mandatory publication signature (CHUNK1b) is configured by four canonical
fields (MAIN/PARTNER text+url): all four belong together, each is validated
offline through ``domain.signature``, and problems name env var + fixed rule
only (values never echoed). Absence keeps offline ``--check`` and run startup
green — publication itself is the demanding stage: the publisher must call
``validate_signature`` BEFORE creating or sending anything (no bypass path;
the planner additionally refuses to plan without a SignatureConfig).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .domain.signature import SignatureConfig, signature_problems

PLACEHOLDER_TOKEN = "REPLACE_WITH_REAL_BOT_TOKEN"

ENV_TOKEN = "POSTER_BOT_BOT_TOKEN"
ENV_OWNER = "POSTER_BOT_OWNER_USER_ID"
ENV_CHANNEL = "POSTER_BOT_CHANNEL_ID"
ENV_DB_PATH = "POSTER_BOT_DB_PATH"
ENV_LOCK_PATH = "POSTER_BOT_LOCK_PATH"
ENV_TZ = "POSTER_BOT_TIMEZONE"
ENV_START = "POSTER_BOT_SLOT_START_HOUR"
ENV_END = "POSTER_BOT_SLOT_END_HOUR"

#: canonical signature fields: every published message ends with
#: '<MAIN_TEXT> | <PARTNER_TEXT>', each label linked to its URL
ENV_SIG_MAIN_TEXT = "POSTER_BOT_SIGNATURE_MAIN_TEXT"
ENV_SIG_MAIN_URL = "POSTER_BOT_SIGNATURE_MAIN_URL"
ENV_SIG_PARTNER_TEXT = "POSTER_BOT_SIGNATURE_PARTNER_TEXT"
ENV_SIG_PARTNER_URL = "POSTER_BOT_SIGNATURE_PARTNER_URL"

_SIG_ENV_BY_FIELD = {
    "main_text": ENV_SIG_MAIN_TEXT,
    "main_url": ENV_SIG_MAIN_URL,
    "partner_text": ENV_SIG_PARTNER_TEXT,
    "partner_url": ENV_SIG_PARTNER_URL,
}
_SIG_ENVS = tuple(_SIG_ENV_BY_FIELD.values())

_DEFAULTS = {
    ENV_OWNER: "0",
    ENV_CHANNEL: "0",
    ENV_DB_PATH: "data/poster_bot.sqlite3",
    ENV_LOCK_PATH: "data/poster_bot.lock",
    ENV_TZ: "Europe/Moscow",
    # Full-day hourly slots by default (owner requirement: 00..23); the
    # window is configurable via env, never silently narrowed.
    ENV_START: "0",
    ENV_END: "23",
}


class ConfigError(ValueError):
    """Structural configuration problems (env var names only, no values)."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class MalformedTokenError(ConfigError):
    """The token is syntactically invalid for the Bot API. Raised by the live
    composition after the durable gate passed; the message names the env var
    only — the token value itself is never echoed anywhere."""

    def __init__(self, env_var: str) -> None:
        super().__init__([f"{env_var} is not a syntactically valid bot token"])


class ChannelResolutionError(ConfigError):
    """A configured ``@channel-username`` could not be resolved to its
    numeric channel id at live startup. Raised by the live composition; the
    message names the env var and a safe reason code only — never the
    username, chat payloads or Telegram's error text."""

    def __init__(self, env_var: str, reason: str) -> None:
        super().__init__([f"{env_var} could not be resolved to a channel ({reason})"])


#: Telegram channel username syntax: 5..32 chars, letter first, then letters,
#: digits and underscores. Leading '@' required for an unambiguous target.
_CHANNEL_USERNAME_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{4,31}")


@dataclass(frozen=True)
class Config:
    # the token never appears in repr()/str(): an accidental log or traceback
    # echo of a Config must not leak it (error messages name env vars only)
    bot_token: str = field(repr=False)
    owner_user_id: int
    # numeric chat id (int, existing semantics) or validated @username (str);
    # a str target is resolved to its numeric id at live startup, never here
    channel_id: int | str
    db_path: str
    lock_path: str
    timezone: str
    slot_start_hour: int
    slot_end_hour: int
    # None = signature fields absent from the environment: offline --check and
    # run startup stay green, publication must refuse (validate_signature).
    # Defaults to None so external Config(...) constructions keep working.
    signature: SignatureConfig | None = None

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def live_problems(self) -> list[str]:
        """Problems that block live mode (never block offline --check)."""
        problems: list[str] = []
        if not self.bot_token or self.bot_token == PLACEHOLDER_TOKEN:
            problems.append(f"{ENV_TOKEN} is missing or still a placeholder")
        if self.owner_user_id <= 0:
            problems.append(f"{ENV_OWNER} must be a positive integer for live mode")
        if isinstance(self.channel_id, int) and self.channel_id == 0:
            problems.append(
                f"{ENV_CHANNEL} must be a non-zero numeric channel id or a "
                "@channel-username for live mode"
            )
        return problems

    def signature_problems(self) -> list[str]:
        """Problems that block PUBLICATION (no signature, no publish).

        Deliberately separate from live_problems(): run startup semantics are
        an existing contract. The publisher calls validate_signature before
        creating or sending anything — there is no optional bypass flag.
        """
        if self.signature is None:
            return [
                f"{name} is missing: every published message must carry the "
                "mandatory '@main | @partner' footer — set all four signature "
                "fields together"
                for name in _SIG_ENVS
            ]
        return []


def _get_int(env: dict[str, str], name: str, problems: list[str]) -> int:
    raw = env.get(name, _DEFAULTS.get(name, ""))
    try:
        return int(raw.strip())
    except ValueError:
        problems.append(f"{name} must be an integer")
        return 0


def _get_channel(env: dict[str, str], name: str, problems: list[str]) -> int | str:
    """Parse the channel target: numeric chat id or validated @username.

    Outer whitespace follows the config's strip convention; inner whitespace,
    URLs and injection characters are rejected. Values are never quoted in
    the problem message (env var name only), matching the token/owner gate.
    """
    raw = env.get(name, _DEFAULTS.get(name, "")).strip()
    if raw.startswith("@"):
        if _CHANNEL_USERNAME_RE.fullmatch(raw):
            return raw
        problems.append(
            f"{name} must be a valid @channel-username "
            "(5..32 letters, digits and underscores, letter first)"
        )
        return 0
    try:
        return int(raw)
    except ValueError:
        problems.append(
            f"{name} must be a numeric channel id or a valid @channel-username"
        )
        return 0


def load_config(env: dict[str, str] | None = None) -> Config:
    """Load and structurally validate config (offline gate).

    Raises ConfigError with all structural problems; values are never quoted.
    """
    source = os.environ if env is None else env
    problems: list[str] = []
    token = source.get(ENV_TOKEN, "").strip()
    owner = _get_int(source, ENV_OWNER, problems)
    channel = _get_channel(source, ENV_CHANNEL, problems)
    db_path = source.get(ENV_DB_PATH, _DEFAULTS[ENV_DB_PATH]).strip()
    lock_path = source.get(ENV_LOCK_PATH, _DEFAULTS[ENV_LOCK_PATH]).strip()
    timezone = source.get(ENV_TZ, _DEFAULTS[ENV_TZ]).strip()
    start = _get_int(source, ENV_START, problems)
    end = _get_int(source, ENV_END, problems)
    if not (0 <= start <= 23 and 0 <= end <= 23 and start <= end):
        problems.append(
            f"{ENV_START}/{ENV_END} must be hours 0..23 with start <= end"
        )
    if not db_path or not lock_path:
        problems.append(f"{ENV_DB_PATH}/{ENV_LOCK_PATH} must not be empty")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(f"{ENV_TZ} is not a loadable IANA timezone")
    signature = _get_signature(source, problems)
    if problems:
        raise ConfigError(problems)
    return Config(
        bot_token=token,
        owner_user_id=owner,
        channel_id=channel,
        db_path=db_path,
        lock_path=lock_path,
        timezone=timezone,
        slot_start_hour=start,
        slot_end_hour=end,
        signature=signature,
    )


def _get_signature(env: dict[str, str], problems: list[str]) -> SignatureConfig | None:
    """Parse the four signature fields: all-or-none, structurally validated.

    Labels trim outer ASCII spaces and remove one matching outer quote pair
    for Compose raw env_file compatibility; internal quotes remain literal.
    Unmatched quotes are literal display text, not a parsing error. Tabs and
    line breaks are never trimmed from labels: validation must reject them.
    URLs retain the existing strip convention, without quote normalization.
    Problem messages are fixed rule texts prefixed
    with the env var name — values are never quoted, matching the
    token/owner/channel gates.
    """
    values = {name: env.get(name, "").strip() for name in _SIG_ENVS}
    for name in (ENV_SIG_MAIN_TEXT, ENV_SIG_PARTNER_TEXT):
        label = env.get(name, "").strip(" ")
        if len(label) >= 2 and label[0] in ("\"", "'") and label[-1] == label[0]:
            label = label[1:-1]
        values[name] = label
    if not any(values.values()):
        return None
    missing = [name for name in _SIG_ENVS if not values[name]]
    if missing:
        problems.extend(
            f"{name} is missing: the signature requires all four fields together"
            for name in missing
        )
        return None
    field_values = {
        field_name: values[env_name]
        for field_name, env_name in _SIG_ENV_BY_FIELD.items()
    }
    rules = signature_problems(**field_values)
    problems.extend(
        f"{_SIG_ENV_BY_FIELD[field]} signature value {rule}" for field, rule in rules
    )
    if rules:
        return None
    return SignatureConfig(**field_values)


def validate_for_live(config: Config) -> None:
    """Raise ConfigError unless config is fit to talk to Telegram."""
    problems = config.live_problems()
    if problems:
        raise ConfigError(problems)


def validate_signature(config: Config) -> None:
    """Raise ConfigError unless every publication can carry the signature.

    The publication path must call this BEFORE creating or sending anything:
    a bot without a configured mandatory footer cannot publish posts at all.
    """
    problems = config.signature_problems()
    if problems:
        raise ConfigError(problems)
