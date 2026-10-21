"""Publication flows: schedule/confirm, edit-pauses, cancel, reservations."""
from __future__ import annotations

import pytest

from conftest import NOW, future, text_content
from poster_bot.domain.content import Content, MediaRef
from poster_bot.domain.errors import (
    ConflictError,
    MinuteTakenError,
    PastTimeError,
    TransitionError,
)
from poster_bot.domain.states import Status


class TestSchedule:
    def test_strictly_future(self, services, scheduled):
        with pytest.raises(PastTimeError):
            services.publications.schedule(
                scheduled[1], future(-5), now_epoch=NOW
            )
        other = services.drafts.create(text_content("x"), now_epoch=NOW)
        with pytest.raises(PastTimeError):
            # boundary: instant == now is "not strictly in the future"
            services.publications.schedule(other, future(0), now_epoch=NOW)

    def test_minute_unique_across_posts(self, services, scheduled):
        pub, _ = scheduled
        other = services.drafts.create(text_content("second"), now_epoch=NOW)
        with pytest.raises(MinuteTakenError):
            services.publications.schedule(
                other, future(120), now_epoch=NOW
            )
        assert pub.minute_epoch == future(120).minute

    def test_one_publication_per_post(self, services, scheduled):
        _, post_id = scheduled
        with pytest.raises(ConflictError):
            services.publications.schedule(post_id, future(300), now_epoch=NOW)

    def test_content_survives_into_publication(self, services):
        post_id = services.drafts.create(
            Content(kind="audio", media=MediaRef("audio", "mp3id", "song")),
            now_epoch=NOW,
        )
        pub = services.publications.schedule(post_id, future(600), now_epoch=NOW)
        assert pub.mode == "copy"
        assert services.drafts.get_content(post_id).media.file_id == "mp3id"


class TestEditPauseConfirm:
    def test_edit_only_while_scheduled_and_pauses(self, services, scheduled):
        pub, post_id = scheduled
        edited = services.publications.edit_content(
            pub.id, pub.version, text_content("edited"), now_epoch=NOW
        )
        assert edited.status is Status.SUSPENDED
        assert services.drafts.get_content(post_id).text == "edited"
        with pytest.raises(TransitionError):
            services.publications.edit_content(
                pub.id, edited.version, text_content("again"), now_epoch=NOW
            )

    def test_edit_stale_version_loses(self, services, scheduled):
        pub, _ = scheduled
        services.publications.pause(pub.id, pub.version, now_epoch=NOW)
        with pytest.raises(TransitionError):
            services.publications.edit_content(
                pub.id, pub.version, text_content("late"), now_epoch=NOW
            )

    def test_resume_requires_future_slot(self, services, scheduled):
        pub, _ = scheduled
        services.publications.pause(pub.id, pub.version, now_epoch=NOW)
        resumed = services.publications.resume(pub.id, pub.version + 1, now_epoch=NOW)
        assert resumed.status is Status.SCHEDULED
        # pause again, move clock past the slot: resume must refuse
        paused = services.publications.pause(
            resumed.id, resumed.version, now_epoch=NOW
        )
        with pytest.raises(PastTimeError):
            services.publications.resume(
                paused.id, paused.version, now_epoch=future(1000).epoch
            )

    def test_suspended_keeps_minute_reserved(self, services, scheduled):
        pub, _ = scheduled
        services.publications.edit_content(
            pub.id, pub.version, text_content("e"), now_epoch=NOW
        )
        other = services.drafts.create(text_content("x"), now_epoch=NOW)
        with pytest.raises(MinuteTakenError):
            services.publications.schedule(other, future(120), now_epoch=NOW)


class TestReplan:
    def test_replan_rejects_past(self, services, scheduled):
        pub, _ = scheduled
        services.publications.pause(pub.id, pub.version, now_epoch=NOW)
        overdue = services.dispatch.recovery_sweep(now_epoch=future(1000).epoch)
        assert pub.id in overdue.missed_publication_ids
        current = services.publications.get(pub.id)
        with pytest.raises(PastTimeError):
            services.publications.replan(
                pub.id, current.version, future(1000), now_epoch=future(2000).epoch
            )

    def test_replan_moves_minute(self, services, scheduled):
        pub, post_id = scheduled
        services.publications.pause(pub.id, pub.version, now_epoch=NOW)
        services.dispatch.recovery_sweep(now_epoch=future(1000).epoch)
        current = services.publications.get(pub.id)
        moved = services.publications.replan(
            pub.id, current.version, future(2000), now_epoch=future(1000).epoch
        )
        assert moved.status is Status.SCHEDULED
        assert moved.minute_epoch == future(2000).minute
        # old minute released, new reserved
        other = services.drafts.create(text_content("x"), now_epoch=NOW)
        services.publications.schedule(
            other, future(1500), now_epoch=future(1000).epoch
        )
        with pytest.raises(MinuteTakenError):
            services.publications.schedule(
                services.drafts.create(text_content("y"), now_epoch=NOW),
                future(2000),
                now_epoch=future(1000).epoch,
            )
        assert post_id is not None


class TestCancel:
    def test_cancel_to_draft_releases_minute(self, services, scheduled):
        pub, post_id = scheduled
        services.publications.cancel_to_draft(pub.id, pub.version, now_epoch=NOW)
        assert services.drafts.is_draft(post_id)
        assert services.publications.get_by_post(post_id) is None
        again = services.publications.schedule(
            services.drafts.create(text_content("x"), now_epoch=NOW),
            future(120),
            now_epoch=NOW,
        )
        assert again.minute_epoch == future(120).minute

    def test_delete_removes_everything(self, services, scheduled):
        pub, post_id = scheduled
        services.publications.delete_post(pub.id, pub.version, now_epoch=NOW)
        assert services.conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE id=?", (post_id,)
        ).fetchone()["c"] == 0
        assert services.publications.get_by_post(post_id) is None
        fresh = services.publications.schedule(
            services.drafts.create(text_content("x"), now_epoch=NOW),
            future(120),
            now_epoch=NOW,
        )
        assert fresh.minute_epoch == future(120).minute

    def test_published_post_deletable_history_goes_too(
        self, services, scheduled
    ):
        pub, post_id = scheduled
        attempt = services.dispatch.claim(
            pub.id, pub.version, now_epoch=pub.scheduled_epoch
        )
        services.dispatch.complete(
            pub.id, attempt, (11,), "https://t.me/c/1/11",
            now_epoch=pub.scheduled_epoch + 5,
        )
        assert len(services.dispatch.history_links(post_id)) == 1
        services.publications.delete_by_post(post_id, now_epoch=NOW + 10)
        assert len(services.dispatch.history_links(post_id)) == 0
        assert services.conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE id=?", (post_id,)
        ).fetchone()["c"] == 0

    def test_in_flight_is_immutable(self, services, scheduled):
        pub, _ = scheduled
        services.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch)
        current = services.publications.get(pub.id)
        with pytest.raises(TransitionError):
            services.publications.cancel_to_draft(
                pub.id, current.version, now_epoch=NOW
            )
        with pytest.raises(TransitionError):
            services.publications.delete_post(pub.id, current.version, now_epoch=NOW)
        with pytest.raises(TransitionError):
            services.publications.edit_content(
                pub.id, current.version, text_content("no"), now_epoch=NOW
            )
        with pytest.raises(TransitionError):
            services.publications.delete_by_post(
                scheduled[1], now_epoch=NOW
            )