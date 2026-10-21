"""Drafts live until deleted and survive restarts (storage-level)."""
from __future__ import annotations

import pytest

from conftest import NOW, text_content
from poster_bot.db import connect
from poster_bot.domain.content import Content, MediaRef
from poster_bot.repo.drafts import DraftNotFound, DraftStore
from poster_bot.journal import Journal


def _reopen_draft_store(services):
    db_path = services.conn.execute("PRAGMA database_list").fetchone()[2]
    services.conn.close()
    conn = connect(db_path)
    return DraftStore(conn), conn


class TestDraftLifecycle:
    def test_create_get_update_delete(self, services):
        store = services.drafts
        post_id = store.create(text_content("one"), now_epoch=NOW)
        assert store.get_content(post_id).text == "one"
        store.update(post_id, text_content("two"), now_epoch=NOW)
        assert store.get_content(post_id).text == "two"
        store.delete(post_id)
        with pytest.raises(DraftNotFound):
            store.get_content(post_id)

    def test_draft_survives_restart(self, services):
        post_id = services.drafts.create(
            Content(kind="album", items=(MediaRef("photo", "p"), MediaRef("video", "v")), caption="keep me"),
            now_epoch=NOW,
        )
        reopened, conn = _reopen_draft_store(services)
        content = reopened.get_content(post_id)
        assert content.caption == "keep me"
        assert [m.file_id for m in content.items] == ["p", "v"]
        assert reopened.is_draft(post_id)
        conn.close()

    def test_scheduled_post_is_not_a_draft(self, services, scheduled):
        _, post_id = scheduled
        assert not services.drafts.is_draft(post_id)
        with pytest.raises(DraftNotFound):
            services.drafts.update(post_id, text_content("sneak"), now_epoch=NOW)
        with pytest.raises(DraftNotFound):
            services.drafts.delete(post_id)

    def test_cancelled_publication_is_draft_again(self, services, scheduled):
        pub, post_id = scheduled
        services.publications.cancel_to_draft(pub.id, pub.version, now_epoch=NOW)
        assert services.drafts.is_draft(post_id)
        services.drafts.update(post_id, text_content("freed"), now_epoch=NOW)
        assert services.drafts.get_content(post_id).text == "freed"

    def test_list_drafts_excludes_publications(self, services, scheduled):
        free = services.drafts.create(text_content("free"), now_epoch=NOW)
        assert services.drafts.list_drafts() == [free]