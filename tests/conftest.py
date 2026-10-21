from __future__ import annotations

from datetime import datetime, timezone

import pytest

from poster_bot.db import connect, migrate
from poster_bot.domain.content import Content
from poster_bot.domain.time import Instant
from poster_bot.journal import Journal
from poster_bot.repo.dispatch import DispatchService
from poster_bot.repo.drafts import DraftStore
from poster_bot.repo.manual_times import ManualTimesRepo
from poster_bot.repo.publications import PublicationsService
from poster_bot.repo.settings import SettingsRepo

#: fake clock: Wed 2026-10-21 08:00 UTC (11:00 Europe/Moscow), no DST edges
NOW = int(datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc).timestamp())


def future(minutes: int = 120) -> Instant:
    return Instant(NOW + minutes * 60)


def text_content(text: str = "hello owner") -> Content:
    return Content(kind="text", text=text)


class Services:
    """Storage-level service bundle over one migrated connection."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.journal = Journal(conn)
        self.drafts = DraftStore(conn)
        self.publications = PublicationsService(conn, self.journal)
        self.dispatch = DispatchService(conn, self.journal)
        self.settings = SettingsRepo(conn)
        self.manual_times = ManualTimesRepo(conn)


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "bot.sqlite3"))
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def services(conn) -> Services:
    return Services(conn)


@pytest.fixture
def scheduled(services: Services):
    """A scheduled publication plus its post id."""
    post_id = services.drafts.create(text_content(), now_epoch=NOW)
    pub = services.publications.schedule(post_id, future(), now_epoch=NOW)
    return pub, post_id