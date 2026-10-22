"""Offline CLI acceptance (Phase 0): `check` creates the DB, validates config
with dummy values, never needs Telegram network, never echoes secrets.
Subprocesses run under the venv python (sys.executable under pytest)."""
from __future__ import annotations

import sqlite3
import subprocess
import sys

import pytest

SECRET = "secret-that-must-not-appear"


def _run(*args, env_file=None):
    argv = [sys.executable, "-m", "poster_bot"]
    if env_file is not None:
        argv += ["--env-file", str(env_file)]
    argv += list(args)
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / "env"
    path.write_text(
        f"POSTER_BOT_BOT_TOKEN={SECRET}\n"
        f"POSTER_BOT_DB_PATH={tmp_path / 'bot.sqlite3'}\n"
        f"POSTER_BOT_LOCK_PATH={tmp_path / 'bot.lock'}\n"
    )
    return path


class TestCheck:
    def test_check_accepts_username_channel_offline(self, tmp_path):
        """@username-канал проходит офлайн-проверку без Telegram-сети (Phase B)."""
        path = tmp_path / "env-username"
        path.write_text(
            f"POSTER_BOT_BOT_TOKEN={SECRET}\n"
            f"POSTER_BOT_DB_PATH={tmp_path / 'u.sqlite3'}\n"
            f"POSTER_BOT_CHANNEL_ID=@test_chan_public2\n"
            f"POSTER_BOT_OWNER_USER_ID=777\n"
        )
        result = _run("check", env_file=path)
        assert result.returncode == 0, result.stderr
        # проверка офлайн: канал НЕ резолвится сетью — только формат
        assert "config is fit for live mode" in result.stdout
        assert SECRET not in result.stdout + result.stderr

    def test_check_rejects_malformed_username_names_only(self, tmp_path):
        for bad in ("@ab", "@1badname", "not-an-id"):
            path = tmp_path / f"env-bad-{bad}"
            path.write_text(
                f"POSTER_BOT_BOT_TOKEN={SECRET}\n"
                f"POSTER_BOT_DB_PATH={tmp_path / 'b.sqlite3'}\n"
                f"POSTER_BOT_CHANNEL_ID={bad}\n"
            )
            result = _run("check", env_file=path)
            assert result.returncode == 2, (bad, result.stdout, result.stderr)
            assert "POSTER_BOT_CHANNEL_ID" in result.stderr
            assert bad not in result.stderr
            assert SECRET not in result.stdout + result.stderr

    def test_check_creates_db_and_passes_with_dummies(self, env_file, tmp_path):
        result = _run("check", env_file=env_file)
        assert result.returncode == 0, result.stderr
        db_path = tmp_path / "bot.sqlite3"
        assert db_path.exists()
        assert (db_path.exists() and sqlite3.connect(db_path).execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='publications'"
        ).fetchone()[0] == 1)
        assert "config structure OK" in result.stdout
        assert "database ready" in result.stdout
        assert SECRET not in result.stdout + result.stderr

    def test_check_alias_flag(self, env_file):
        assert _run("--check", env_file=env_file).returncode == 0

    def test_structural_failure_exit_code(self, tmp_path):
        path = tmp_path / "env"
        path.write_text("POSTER_BOT_SLOT_START_HOUR=30\n")
        result = _run("check", env_file=path)
        assert result.returncode == 2
        assert "POSTER_BOT_SLOT_START_HOUR" in result.stderr

    def test_dummy_live_notes_are_informational(self, env_file):
        result = _run("check", env_file=env_file)
        assert "live mode note" in result.stdout


class TestRunGate:
    def test_run_blocks_dummy_config(self, env_file):
        result = _run("run", env_file=env_file)
        # token in the fixture is a dummy non-placeholder value: owner and
        # channel keep the run behind the live gate; values are never echoed
        assert result.returncode == 3
        assert "POSTER_BOT_OWNER_USER_ID" in result.stderr
        assert "POSTER_BOT_CHANNEL_ID" in result.stderr
        assert SECRET not in result.stderr + result.stdout

    def test_run_without_command_usage(self, env_file):
        assert _run(env_file=env_file).returncode == 2


BAD_LIVE_TOKEN = "NOCOLON_SUPERSECRET_VALUE"


class TestRunTokenGate:
    def _live_env(self, tmp_path, token):
        path = tmp_path / "env_live"
        path.write_text(
            f"POSTER_BOT_BOT_TOKEN={token}\n"
            f"POSTER_BOT_OWNER_USER_ID=111\n"
            f"POSTER_BOT_CHANNEL_ID=-100222\n"
            f"POSTER_BOT_DB_PATH={tmp_path / 'live.sqlite3'}\n"
            f"POSTER_BOT_LOCK_PATH={tmp_path / 'live.lock'}\n"
        )
        return path

    def test_malformed_token_named_not_echoed_and_lock_released(self, tmp_path):
        env = self._live_env(tmp_path, BAD_LIVE_TOKEN)
        result = _run("run", env_file=env)
        assert result.returncode == 3
        assert "POSTER_BOT_BOT_TOKEN" in result.stderr
        assert BAD_LIVE_TOKEN not in result.stderr + result.stdout
        # second run is refused for the SAME token reason (3), never by the
        # lock (4): the failed open must release the singleton lock
        second = _run("run", env_file=env)
        assert second.returncode == 3
        assert "lock" not in second.stderr.lower()


class TestNoTelegramImport:
    def _probe(self, import_list: str) -> str:
        code = f"import sys, {import_list};print('aiogram' in sys.modules)"
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def test_app_and_cli_do_not_import_aiogram(self):
        assert self._probe("poster_bot.app, poster_bot.__main__") == "False"

    def test_runtime_composition_does_not_import_aiogram(self):
        modules = (
            "poster_bot.runtime, poster_bot.repo.sender,"
            "poster_bot.transport, poster_bot.repo.processed_updates"
        )
        assert self._probe(modules) == "False"

    def test_polling_core_is_aiogram_free(self):
        # the durable loop core never loads aiogram: only live.py does
        assert self._probe("poster_bot.polling") == "False"

    def test_adapter_is_the_aiogram_boundary(self):
        # positive control: the adapter really is where aiogram enters
        assert self._probe("poster_bot.transport.aiogram_channel") == "True"