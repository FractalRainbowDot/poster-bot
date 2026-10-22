"""Durable UI plumbing: conversation state CAS, one-shot action tokens,
durable inbox (begin/complete, offset safety). Offline only."""
from __future__ import annotations

import pytest
from conftest import NOW

from poster_bot.db import connect, migrate
from poster_bot.repo.inbox import InboxRepo, InboxStatus
from poster_bot.ui.actions import ActionStore, ActionStale
from poster_bot.ui.state import State, StaleStateError, UiStateRepo


class TestUiStateRepo:
    def test_fresh_state_is_menu(self, conn):
        ui = UiStateRepo(conn).get()
        assert ui.state is State.MENU

    def test_set_increments_version_and_round_trips_payload(self, conn):
        repo = UiStateRepo(conn)
        first = repo.set(State.CREATING, {"staged": {"kind": "text", "text": "x"}},
                         now_epoch=NOW)
        assert first.state is State.CREATING
        assert first.arg("staged")["text"] == "x"
        second = repo.set(State.MANUAL_TIME, {"post_id": 3},
                          expected_version=first.version, now_epoch=NOW + 1)
        assert second.version == first.version + 1
        assert second.arg("post_id") == 3
        assert second.arg("missing", "dflt") == "dflt"

    def test_wrong_expected_version_fails_closed(self, conn):
        repo = UiStateRepo(conn)
        repo.set(State.CREATING, {}, now_epoch=NOW)
        with pytest.raises(StaleStateError):
            repo.set(State.MENU, {}, expected_version=99, now_epoch=NOW)
        # the failed write must not have moved the conversation
        assert repo.get().state is State.CREATING

    def test_state_survives_reopen(self, conn, tmp_path):
        repo = UiStateRepo(conn)
        saved = repo.set(State.AWAIT_REPLACE,
                         {"prev_state": "creating",
                          "pending_replace": {"kind": "media"}},
                         now_epoch=NOW)
        conn.close()
        reopened = connect(str(tmp_path / "bot.sqlite3"))
        migrate(reopened)
        ui = UiStateRepo(reopened).get()
        assert ui.state is State.AWAIT_REPLACE
        assert ui.version == saved.version
        assert ui.arg("pending_replace") == {"kind": "media"}

    def test_reset_to_menu_clears_payload(self, conn):
        repo = UiStateRepo(conn)
        repo.set(State.EDITING, {"post_id": 1}, now_epoch=NOW)
        ui = repo.reset_to_menu(now_epoch=NOW)
        assert ui.state is State.MENU
        assert ui.payload == {}


class TestActionStore:
    def test_consume_exactly_once(self, conn):
        store = ActionStore(conn)
        token = store.create("confirm_schedule", post_id=4,
                             expected_version=2, minute_epoch=NOW + 3600,
                             now_epoch=NOW)
        action = store.consume(token, now_epoch=NOW)
        assert action.action == "confirm_schedule"
        assert action.post_id == 4
        assert action.expected_version == 2
        assert action.minute_epoch == NOW + 3600
        with pytest.raises(ActionStale):
            store.consume(token, now_epoch=NOW + 1)

    def test_unknown_token_is_stale(self, conn):
        with pytest.raises(ActionStale):
            ActionStore(conn).consume("a0000000000000", now_epoch=NOW)

    def test_peek_does_not_consume(self, conn):
        store = ActionStore(conn)
        token = store.create("back", now_epoch=NOW)
        assert store.peek(token) is not None
        assert store.peek(token).status == "pending"
        assert store.consume(token, now_epoch=NOW).status == "consumed"

    def test_replayed_press_after_restart_stays_stale(self, conn, tmp_path):
        store = ActionStore(conn)
        token = store.create("confirm_schedule", publication_id=9, now_epoch=NOW)
        store.consume(token, now_epoch=NOW)
        conn.close()
        reopened = connect(str(tmp_path / "bot.sqlite3"))
        migrate(reopened)
        with pytest.raises(ActionStale):
            ActionStore(reopened).consume(token, now_epoch=NOW + 60)

    def test_invalidate_supersedes_pending_targets(self, conn):
        store = ActionStore(conn)
        keep = store.create("pub_open", publication_id=1, now_epoch=NOW)
        old = store.create("pub_pause", publication_id=1, now_epoch=NOW)
        other = store.create("pub_pause", publication_id=2, now_epoch=NOW)
        assert store.invalidate(publication_id=1, except_token=keep) == 1
        with pytest.raises(ActionStale):
            store.consume(old, now_epoch=NOW)
        assert store.consume(keep, now_epoch=NOW).status == "consumed"
        assert store.consume(other, now_epoch=NOW).action == "pub_pause"


class TestInboxRepo:
    def test_begin_complete_lifecycle(self, conn):
        inbox = InboxRepo(conn)
        assert inbox.begin(10, now_epoch=NOW) is InboxStatus.NEW
        # crash before complete: same update arrives again -> redo, not lost
        assert inbox.begin(10, now_epoch=NOW + 5) is InboxStatus.REDO
        assert inbox.pending_ids() == [10]
        inbox.complete(10, now_epoch=NOW + 6)
        assert inbox.is_done(10)
        assert inbox.begin(10, now_epoch=NOW + 7) is InboxStatus.DONE
        inbox.complete(10, now_epoch=NOW + 8)  # idempotent
        assert inbox.pending_ids() == []

    def test_safe_offset_never_skips_pending(self, conn):
        inbox = InboxRepo(conn)
        assert inbox.safe_offset() is None  # nothing received yet
        inbox.begin(10)
        inbox.complete(10)
        inbox.begin(11)          # pending (mid-handling)
        inbox.begin(12)
        inbox.complete(12)
        # offset 11 keeps update 11 in Telegram's backlog for redelivery
        assert inbox.safe_offset() == 11
        inbox.complete(11)
        assert inbox.safe_offset() == 13

    def test_rejects_non_positive_update_id(self, conn):
        with pytest.raises(ValueError):
            InboxRepo(conn).begin(0)

    def test_backlog_survives_reopen(self, conn, tmp_path):
        inbox = InboxRepo(conn)
        inbox.begin(3)
        inbox.begin(4)
        inbox.complete(3)
        conn.close()
        reopened = connect(str(tmp_path / "bot.sqlite3"))
        migrate(reopened)
        fresh = InboxRepo(reopened)
        assert fresh.pending_ids() == [4]
        assert fresh.safe_offset() == 4
        assert fresh.begin(4) is InboxStatus.REDO
