"""Sender flow over the fake transport: one durable outcome per due post."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace

import pytest

from conftest import NOW, Instant
from fake_transport import FakeTransport
from poster_bot.domain.content import (
    Content,
    Entity,
    ForwardSource,
    MediaRef,
)
from poster_bot.domain.errors import TransitionError
from poster_bot.repo.sender import SenderService

from conftest import text_content


def run(coro):
    return asyncio.run(coro)


def schedule(services, content, epoch: int):
    post_id = services.drafts.create(content, now_epoch=NOW)
    return services.publications.schedule(post_id, Instant(epoch), now_epoch=NOW)


def status_of(services, pub_id: int) -> str:
    return services.conn.execute(
        "SELECT status FROM publications WHERE id=?", (pub_id,)
    ).fetchone()["status"]


def attempt_of(services, pub_id: int):
    return services.conn.execute(
        "SELECT state, outcome FROM send_attempts WHERE publication_id=?"
        " ORDER BY id DESC LIMIT 1",
        (pub_id,),
    ).fetchone()


def journal_kinds(services) -> list[str]:
    return [
        row["kind"]
        for row in services.conn.execute(
            "SELECT kind FROM journal_events ORDER BY id"
        )
    ]


PHOTO = Content(kind="photo", media=MediaRef("photo", "ph1", caption="cap"))
VIDEO = Content(kind="video", media=MediaRef("video", "vd1", caption="rec"))
AUDIO = Content(kind="audio", media=MediaRef("audio", "au1", caption="song"))
ALBUM = Content(
    kind="album",
    items=(MediaRef("photo", "p1"), MediaRef("video", "v1")),
    caption="album caption",
)
FORWARD = Content(
    kind="forward",
    mode="forward",
    forward=ForwardSource(chat_id=-100555, message_ids=(11, 12)),
)


class TestHappyPath:
    @pytest.mark.parametrize("content", [PHOTO, VIDEO, AUDIO, ALBUM, FORWARD],
                             ids=["photo", "video", "audio", "album", "forward"])
    def test_every_kind_publishes_with_durable_history(self, services, content):
        pub = schedule(services, content, NOW + 7200)
        transport = FakeTransport()
        sender = SenderService(services.dispatch, transport)
        results = run(sender.send_due(now_epoch=pub.scheduled_epoch))

        assert [r.outcome for r in results] == ["published"]
        assert transport.calls[0].kind == content.kind
        assert status_of(services, pub.id) == "published"
        att = attempt_of(services, pub.id)
        assert (att["state"], att["outcome"]) == ("finished", "published")
        history = services.conn.execute(
            "SELECT channel_message_ids, permalink FROM published_history"
            " WHERE publication_id=?",
            (pub.id,),
        ).fetchone()
        assert json.loads(history["channel_message_ids"])
        assert history["permalink"].startswith("https://t.me/")

    def test_formatting_survives_the_storage_round_trip(self, services):
        content = replace(
            text_content("read 𝐭𝐡𝐢s"),
            entities=(
                Entity(type="bold", offset=5, length=6),
                Entity(type="text_mention", offset=0, length=4,
                       user_id=42, username="ann"),
            ),
        )
        pub = schedule(services, content, NOW + 7200)
        transport = FakeTransport()
        run(SenderService(services.dispatch, transport).send_due(
            now_epoch=pub.scheduled_epoch
        ))
        # identical payload reached the transport boundary, entities intact
        assert transport.calls[0] == content

    def test_each_minute_window_sends_only_its_own_post(self, services):
        # the one-post-per-minute rule means at most one publication is ever
        # inside a healthy window at the same instant
        first = schedule(services, text_content("first"), NOW + 3600)
        schedule(services, text_content("second"), NOW + 3660)
        transport = FakeTransport()
        sender = SenderService(services.dispatch, transport)
        results = run(sender.send_due(now_epoch=first.scheduled_epoch + 1))
        assert [r.publication_id for r in results] == [first.id]
        results = run(sender.send_due(now_epoch=first.scheduled_epoch + 61))
        assert [r.publication_id for r in results] == [first.id + 1]
        assert [c.text for c in transport.calls] == ["first", "second"]


class TestFailureClassification:
    def _fail(self, services, content, outcome):
        pub = schedule(services, content, NOW + 7200)
        transport = FakeTransport()
        transport.queue(outcome)
        results = run(SenderService(services.dispatch, transport).send_due(
            now_epoch=pub.scheduled_epoch
        ))
        return pub, results

    def test_definitive_api_error_parks_missed_not_rescheduled(self, services):
        from poster_bot.transport.ports import DefinitiveFailure

        pub, results = self._fail(
            services, text_content(), DefinitiveFailure("api_error")
        )
        assert results[0].outcome == "missed"
        assert status_of(services, pub.id) == "missed"
        att = attempt_of(services, pub.id)
        assert (att["state"], att["outcome"]) == ("finished", "definitive_failure")
        kinds = journal_kinds(services)
        assert "send_failed_definitive" in kinds
        row = services.conn.execute(
            "SELECT detail FROM journal_events"
            " WHERE kind='send_failed_definitive'"
        ).fetchone()
        detail = json.loads(row["detail"])
        assert detail["code"] == "api_error"  # allowlisted code, no payloads
        assert set(detail) <= {"code", "publication_id", "post_id"}

    def test_rate_limited_is_definitive_no_auto_retry(self, services):
        from poster_bot.transport.ports import DefinitiveFailure

        pub, results = self._fail(
            services, text_content(), DefinitiveFailure("rate_limited")
        )
        assert status_of(services, pub.id) == "missed"

    def test_timeout_parks_uncertain_and_abandons_attempt(self, services):
        from poster_bot.transport.ports import UncertainOutcome

        pub, results = self._fail(
            services, text_content(), UncertainOutcome("timeout")
        )
        assert results[0].outcome == "uncertain"
        assert status_of(services, pub.id) == "uncertain"
        att = attempt_of(services, pub.id)
        assert att["state"] == "abandoned"

    def test_partial_forward_parks_uncertain(self, services):
        from poster_bot.transport.ports import UncertainOutcome

        pub, results = self._fail(
            services, FORWARD, UncertainOutcome("partial_result")
        )
        assert status_of(services, pub.id) == "uncertain"

    def test_unclassified_code_is_rejected_before_any_write(self, services):
        from poster_bot.transport.ports import UncertainOutcome

        pub = schedule(services, text_content(), NOW + 7200)
        transport = FakeTransport()
        transport.queue(UncertainOutcome("oops_leaky_code"))
        sender = SenderService(services.dispatch, transport)
        with pytest.raises(TransitionError):
            run(sender.send_due(now_epoch=pub.scheduled_epoch))
        # the attempt is durable but the publication stays unresolved:
        # recovery will surface it, nothing silently reclassifies it
        assert status_of(services, pub.id) == "sending"

    def test_adapter_crash_leaves_durable_uncertain_then_propagates(self, services):
        pub = schedule(services, text_content(), NOW + 7200)
        transport = FakeTransport()
        transport.queue(RuntimeError("adapter bug"))
        sender = SenderService(services.dispatch, transport)
        with pytest.raises(RuntimeError):
            run(sender.send_due(now_epoch=pub.scheduled_epoch))
        assert status_of(services, pub.id) == "uncertain"
        assert attempt_of(services, pub.id)["state"] == "abandoned"

    def test_adapter_crash_log_never_carries_exception_text(self, services, caplog):
        # a transport crash may embed post content or token fragments in the
        # exception message; the log line must carry the class name ONLY
        sentinel = "PRIVATE-POST-TEXT 123:TOKEN-SECRET-VALUE"
        pub = schedule(services, text_content(f"body {sentinel}"), NOW + 7200)
        transport = FakeTransport()
        transport.queue(RuntimeError(f"adapter died sending {sentinel!r}"))
        sender = SenderService(services.dispatch, transport)
        with caplog.at_level(logging.DEBUG, logger="poster_bot.repo.sender"):
            with pytest.raises(RuntimeError):
                run(sender.send_due(now_epoch=pub.scheduled_epoch))
        # durable resolution is untouched by the logging change
        assert status_of(services, pub.id) == "uncertain"
        records = [r for r in caplog.records if r.name == "poster_bot.repo.sender"]
        assert records, "the crash must still be logged"
        for record in records:
            assert sentinel not in record.getMessage()
            assert sentinel not in (record.exc_text or "")
            assert record.exc_info is None  # no traceback may ever be attached
        assert sentinel not in caplog.text
        assert any("RuntimeError" in r.getMessage() for r in records)
        assert any(r.levelno == logging.ERROR for r in records)


class TestWindowDiscipline:
    def test_early_clock_never_sends(self, services):
        schedule(services, text_content(), NOW + 7200)
        transport = FakeTransport()
        results = run(SenderService(services.dispatch, transport).send_due(
            now_epoch=NOW
        ))
        assert results == []
        assert transport.calls == []

    def test_past_window_never_replays_after_reconnect(self, services):
        pub = schedule(services, text_content(), NOW + 7200)
        transport = FakeTransport()
        sender = SenderService(services.dispatch, transport)
        # clock jumped past the minute: no send, post waits for the sweep
        run(sender.send_due(now_epoch=pub.scheduled_epoch + 60))
        assert transport.calls == []
        assert status_of(services, pub.id) == "scheduled"
        services.dispatch.recovery_sweep(now_epoch=pub.scheduled_epoch + 61)
        assert status_of(services, pub.id) == "missed"
        run(sender.send_due(now_epoch=pub.scheduled_epoch + 61))
        assert transport.calls == []  # missed posts are never picked up

    def test_failed_post_is_not_resent_on_next_tick(self, services):
        from poster_bot.transport.ports import DefinitiveFailure

        pub = schedule(services, text_content(), NOW + 7200)
        transport = FakeTransport()
        transport.queue(DefinitiveFailure("api_error"))
        sender = SenderService(services.dispatch, transport)
        run(sender.send_due(now_epoch=pub.scheduled_epoch))
        run(sender.send_due(now_epoch=pub.scheduled_epoch))  # same minute
        assert len(transport.calls) == 1  # only the claim won once