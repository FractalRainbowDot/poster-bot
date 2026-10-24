"""Startup shares its resolved numeric destination with the native sender."""
import asyncio
from types import SimpleNamespace

from conftest import NOW, Services
from planned_helpers import SIG
from test_original_forward_dispatch import native_publication
from test_original_forward_sender import FakeAPI
from poster_bot.config import Config
from poster_bot.db import connect, migrate
from poster_bot.live import compose_live


def test_live_resolved_channel_is_trusted_native_runtime_context(tmp_path, caplog):
    path = str(tmp_path / "isolated.sqlite3")
    conn = connect(path)
    migrate(conn)
    pub, _ = native_publication(Services(conn))
    conn.close()
    config = Config(bot_token="123:abcdefghij", owner_user_id=123,
        channel_id="@native_channel", db_path=path,
        lock_path=str(tmp_path / "isolated.lock"), timezone="Europe/Moscow",
        slot_start_hour=0, slot_end_hour=23, signature=SIG)

    class Bot(FakeAPI):
        def __init__(self):
            super().__init__(None)
            self.resolutions = []
            self.closed = False
            self.session = SimpleNamespace(close=self.close)

        async def close(self):
            self.closed = True

        async def get_chat(self, **kwargs):
            self.resolutions.append(kwargs)
            return SimpleNamespace(type="channel", id=-100999)

    async def scenario():
        bot = Bot()
        live = await compose_live(config, bot=bot, clock=lambda: NOW, now_epoch=NOW)
        try:
            bot.services = SimpleNamespace(conn=live.app.conn)
            report = await live.runtime.tick(now_epoch=pub.scheduled_epoch)
            assert [result.outcome for result in report.sent] == ["published"]
            assert bot.resolutions == [{"chat_id": "@native_channel"}]
            assert bot.calls == [{"chat_id": -100999, "from_chat_id": 123,
                                  "message_ids": [11, 12, 13]}]
            assert "migration readiness blocker" not in caplog.text
        finally:
            await live.close()
        assert bot.closed

    asyncio.run(scenario())
