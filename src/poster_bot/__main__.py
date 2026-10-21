"""CLI entrypoint.

`poster-bot check` — fully offline: validates config structure (dummy values
accepted), creates/migrates the SQLite file, seeds settings, reports live-mode
problems without failing on them. No Telegram network, ever, and this path
never imports aiogram.
`poster-bot run` — the durable poller: live gate, singleton lock, startup
recovery sweep, then getUpdates polling with mark-after-persist handling and
runtime ticks (see poster_bot.polling). Exit codes: 0 clean stop/interrupt,
2 usage, 3 live-config or fatal-token problem, 4 lock refused,
5 unexpected runtime failure.
"""
from __future__ import annotations

import argparse
import sys
import time

from .app import open_offline
from .config import ConfigError, load_config
from .singleton import AlreadyRunningError

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_LIVE_CONFIG = 3
EXIT_ALREADY_RUNNING = 4
EXIT_RUNTIME = 5


def _wall_clock() -> int:
    return int(time.time())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="poster-bot",
        description="Poster bot: local storage, config and offline checks.",
    )
    parser.add_argument(
        "--env-file",
        help="optional KEY=VALUE file to load before checking (never logged)",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="offline config + database check (no network)")
    sub.add_parser("run", help="live start gate (no Telegram UI until phase 2)")
    parser.add_argument(
        "--check",
        dest="check_flag",
        action="store_true",
        help="alias for the 'check' subcommand",
    )
    return parser


def _read_env_file(path: str) -> dict[str, str]:
    import os

    env = dict(os.environ)
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or ("check" if args.check_flag else None)
    if command is None:
        print("no command: use 'check' or 'run'", file=sys.stderr)
        return EXIT_USAGE
    env = _read_env_file(args.env_file) if args.env_file else None
    try:
        config = load_config(env)
    except ConfigError as exc:
        for problem in exc.problems:
            print(f"config: {problem}", file=sys.stderr)
        return EXIT_USAGE

    if command == "check":
        app = open_offline(config)
        try:
            problems = config.live_problems()
            print("offline check: config structure OK")
            print(f"offline check: database ready at {config.db_path}")
            for problem in problems:
                print(f"live mode note: {problem}")
            if not problems:
                print("live mode note: config is fit for live mode")
            return EXIT_OK
        finally:
            app.close()

    # command == "run": the real durable poller. Imported lazily so 'check'
    # never touches aiogram (enforced by tests).
    try:
        import asyncio

        from .live import run_live

        def _startup(pending_ids: tuple[int, ...]) -> None:
            print("run: config valid, lock acquired, database migrated, recovery sweep done")
            if pending_ids:
                # ids only — notices carry no content, ids are not secrets
                print(f"run: owner notices awaiting delivery: {list(pending_ids)}")
            print("run: polling starts now; channel posts happen only inside schedule windows")

        asyncio.run(
            run_live(
                config,
                clock=_wall_clock,
                on_started=_startup,
            )
        )
        print("run: stopped cleanly")
        return EXIT_OK
    except ConfigError as exc:
        for problem in exc.problems:
            print(f"config: {problem}", file=sys.stderr)
        return EXIT_LIVE_CONFIG
    except AlreadyRunningError:
        print("run refused: another poster-bot process holds the lock", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except KeyboardInterrupt:
        # signal handlers normally make shutdown graceful; this is the
        # fallback when a signal raced asyncio.run's setup
        print("run: interrupted")
        return EXIT_OK
    except Exception as exc:
        # only the class name is reported: no tracebacks, no internals,
        # nothing that could echo a token
        from .polling import PollFatal

        if isinstance(exc, PollFatal):
            print(f"run stopped: Telegram rejected this bot token (code={exc.code});"
                  " check POSTER_BOT_BOT_TOKEN", file=sys.stderr)
            return EXIT_LIVE_CONFIG
        print(f"run stopped: unexpected {type(exc).__name__}", file=sys.stderr)
        return EXIT_RUNTIME


if __name__ == "__main__":
    raise SystemExit(main())