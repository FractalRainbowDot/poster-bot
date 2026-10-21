"""Journal: 30-day retention; no post content or secrets can be stored."""
from __future__ import annotations

import json

import pytest

from conftest import NOW, text_content
from poster_bot.journal import RETENTION_DAYS, JournalRedactionError

DAY = 24 * 3600


class TestRetention:
    def test_prune_boundary_30_days(self, services):
        journal = services.journal
        journal.record("draft_created", now_epoch=NOW - 29 * DAY)
        journal.record("draft_created", now_epoch=NOW - 31 * DAY)
        journal.record("draft_created", now_epoch=NOW)
        removed = journal.prune(now_epoch=NOW)
        assert removed == 1
        assert len(journal.entries(limit=10)) == 2

    def test_prune_exact_boundary_kept(self, services):
        services.journal.record("draft_created", now_epoch=NOW - RETENTION_DAYS * DAY)
        assert services.journal.prune(now_epoch=NOW) == 0


class TestRedaction:
    def test_unknown_kind_rejected(self, services):
        with pytest.raises(JournalRedactionError):
            services.journal.record("owner_whisper")

    def test_unallowed_key_rejected(self, services):
        with pytest.raises(JournalRedactionError):
            services.journal.record("draft_created", caption="пост с котиками")

    def test_free_form_strings_rejected(self, services):
        with pytest.raises(JournalRedactionError):
            services.journal.record("published", message_count=" секретное слово")
        with pytest.raises(JournalRedactionError):
            services.journal.record(
                "settings_changed", keys_changed="Europe/Observed"
            )

    def test_safe_values_accepted(self, services):
        services.journal.record(
            "settings_changed", keys_changed="timezone", now_epoch=NOW
        )
        services.journal.record("attempt_finished", outcome="published")

    def test_stored_rows_have_no_content_or_tokens(self, services, scheduled):
        pub, post_id = scheduled
        post_id2 = services.drafts.create(
            text_content("никакого секрета REPLACE_WITH_REAL_BOT_TOKEN"),
            now_epoch=NOW,
        )
        rows = services.conn.execute(
            "SELECT detail FROM journal_events"
        ).fetchall()
        blob = "\n".join(row["detail"] for row in rows)
        for row in rows:
            json.loads(row["detail"])  # structured detail only
        assert "никакого секрета" not in blob
        assert "REPLACE" not in blob
        assert post_id is not None and post_id2 is not None