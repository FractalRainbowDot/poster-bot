"""Environment-backed configuration.

Errors never echo secret values: problem messages reference env var names only.
Offline mode accepts dummy values; live mode rejects placeholder/missing
token, non-positive owner id, zero channel id.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PLACEHOLDER_TOKEN = "REPLACE_WITH_REAL_BOT_TOKEN"

ENV_TOKEN = "POSTER_BOT_BOT_TOKEN"
ENV_OWNER = "POSTER_BOT_OWNER_USER_ID"
ENV_CHANNEL = "POSTER_BOT_CHANNEL_ID"
ENV_DB_PATH = "POSTER_BOT_DB_PATH"
ENV_LOCK_PATH = "POSTER_BOT_LOCK_PATH"
ENV_TZ = "POSTER_BOT_TIMEZONE"
ENV_START = "POSTER_BOT_SLOT_START_HOUR"
ENV_END = "POSTER_BOT_SLOT_END_HOUR"

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


@dataclass(frozen=True)
class Config:
    # the token never appears in repr()/str(): an accidental log or traceback
    # echo of a Config must not leak it (error messages name env vars only)
    bot_token: str = field(repr=False)
    owner_user_id: int
    channel_id: int
    db_path: str
    lock_path: str
    timezone: str
    slot_start_hour: int
    slot_end_hour: int

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def live_problems(self) -> list[str]:
        """Problems that block live mode (never block offline --check)."""
        problems: list[str] = []
        if not self.bot_token or self.bot_token == PLACEHOLDER_TOKEN:
            problems.append(f"{ENV_TOKEN} is missing or still a placeholder")
        if self.owner_user_id <= 0:
            problems.append(f"{ENV_OWNER} must be a positive integer for live mode")
        if self.channel_id == 0:
            problems.append(f"{ENV_CHANNEL} must be a non-zero integer for live mode")
        return problems


def _get_int(env: dict[str, str], name: str, problems: list[str]) -> int:
    raw = env.get(name, _DEFAULTS.get(name, ""))
    try:
        return int(raw.strip())
    except ValueError:
        problems.append(f"{name} must be an integer")
        return 0


def load_config(env: dict[str, str] | None = None) -> Config:
    """Load and structurally validate config (offline gate).

    Raises ConfigError with all structural problems; values are never quoted.
    """
    source = os.environ if env is None else env
    problems: list[str] = []
    token = source.get(ENV_TOKEN, "").strip()
    owner = _get_int(source, ENV_OWNER, problems)
    channel = _get_int(source, ENV_CHANNEL, problems)
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
    )


def validate_for_live(config: Config) -> None:
    """Raise ConfigError unless config is fit to talk to Telegram."""
    problems = config.live_problems()
    if problems:
        raise ConfigError(problems)