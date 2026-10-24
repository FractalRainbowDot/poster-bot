"""Redelivery idempotency (crash between the flow's committed effect and
inbox.complete): Telegram re-sends the same incoming update and the UI layer
must replay it durably — never duplicate intake effects, never raise a false
conflict, never lose the pending choice. Durable means it also holds across a
restart (reopen), because the dedup marker lives in the database.
"""
from __future__ import annotations

import pytest
from conftest import NOW, Services
from ui_flow_helpers import build_flow, find_token, labels, press, press_label, reopen, run

import poster_bot.ui.texts as texts
from poster_bot.domain.content import Entity
from poster_bot.ui.state import UiStateRepo

CHAT_KEY = "-1007:55"  # chat_id:message_id-shaped effect key fixture value


def _stage_text(flow, text: str = "первый", *, key: str | None = "c:1"):
    return run(flow.text(now=NOW, text=text, source_key=key))


class TestMessageDedupSurvivesRestart:
    def test_redelivered_text_does_not_double_stage(self, services, tmp_path):
        flow, _ = build_flow(services)
        run(flow.new_post(now=NOW))
        staged = _stage_text(flow)
        assert "Текст (6)" in staged.text
        # crash here: effect committed, inbox.complete never ran
        restarted, flow2, _ = reopen(services, tmp_path)
        replay = _stage_text(flow2)
        # same durable effect key: the staged content stays exactly as it was
        assert replay.text == staged.text
        assert texts.REPLACE_ASK not in replay.text
        view = UiStateRepo(restarted.conn).get()
        assert view.state.name == "CREATING"
        assert view.arg("staged")["text"] == "первый"

    def test_redelivered_album_item_does_not_duplicate(self, services, tmp_path):
        flow, _ = build_flow(services)
        run(flow.new_post(now=NOW))
        run(flow.media(now=NOW, kind="photo", file_id="p1", caption=None,
                       media_group_id="g1", source_key="c:1"))
        second = run(flow.media(now=NOW, kind="video", file_id="v1",
                                caption=None, media_group_id="g1",
                                source_key="c:2"))
        assert "Альбом (2)" in second.text
        token = find_token(second, texts.BTN_CONTINUE)
        restarted, flow2, preview2 = reopen(services, tmp_path)
        replay = run(flow2.media(now=NOW + 5, kind="video", file_id="v1",
                                 caption=None, media_group_id="g1",
                                 source_key="c:2"))
        # count unchanged, no conflict, no duplicate item
        assert "Альбом (2)" in replay.text
        created = press(flow2, token, now=NOW + 60)
        assert texts.DRAFT_SAVED in created.text
        content = restarted.drafts.get_content(flow2._ui().arg('post_id'))
        assert [i.file_id for i in content.items] == ["p1", "v1"]
        assert len(preview2.owner_previews) == 1

    def test_redelivered_forward_does_not_duplicate_ids(self, services, tmp_path):
        from poster_bot.ui.staged import copy_candidate_from_media

        flow, _ = build_flow(services)
        run(flow.new_post(now=NOW))
        candidate = copy_candidate_from_media("photo", "ph", None, (), None)
        run(flow.forwarded(now=NOW, src_chat_id=-100999, src_message_id=42,
                           media_group_id=None, copy_candidate=candidate,
                           source_key="c:9"))
        restarted, flow2, _ = reopen(services, tmp_path)
        replay = run(flow2.forwarded(
            now=NOW + 5, src_chat_id=-100999, src_message_id=42,
            media_group_id=None,
            copy_candidate=copy_candidate_from_media("photo", "ph", None,
                                                     (), None),
            source_key="c:9"))
        assert replay.text == texts.FORWARD_ASK  # same ask, not a conflict
        view = UiStateRepo(restarted.conn).get()
        assert view.arg("staged")["forward_ids"] == [42]


class TestDedupOfDecisionAsks:
    def test_redelivered_caption_ask_stays_a_caption_ask(self, services):
        flow, _ = build_flow(services)
        run(flow.new_post(now=NOW))
        run(flow.media(now=NOW, kind="photo", file_id="ph1", caption=None,
                       media_group_id=None, source_key="c:1"))
        ask = run(flow.text(now=NOW, text=" подпись ", source_key="c:2"))
        assert ask.text == texts.CAPTION_ASK
        replay = run(flow.text(now=NOW + 5, text=" подпись ",
                               source_key="c:2"))
        assert replay.text == texts.CAPTION_ASK
        assert texts.BTN_AS_CAPTION in labels(replay)
        view = UiStateRepo(services.conn).get()
        assert view.arg("pending_caption")["text"] == " подпись "
        assert view.arg("pending_replace") is None
        # the fresh replay buttons still work
        done = press_label(flow, replay, texts.BTN_AS_CAPTION, now=NOW + 10)
        assert "Фото" in done.text

    def test_redelivered_conflict_never_rewrites_candidate(self, services):
        flow, _ = build_flow(services)
        run(flow.new_post(now=NOW))
        _stage_text(flow, "первый", key="c:1")
        ask = _stage_text(flow, "второй вариант", key="c:2")
        assert ask.text == texts.REPLACE_ASK
        replay = _stage_text(flow, "второй вариант", key="c:2")
        assert replay.text == texts.REPLACE_ASK
        view = UiStateRepo(services.conn).get()
        assert view.arg("pending_replace")["text"] == "второй вариант"


class TestReplayAfterConversationMoved:
    def test_replay_after_continue_answers_without_destruction(self, services):
        flow, _ = build_flow(services)
        run(flow.new_post(now=NOW))
        staged = _stage_text(flow)
        press(flow, find_token(staged, texts.BTN_CONTINUE), now=NOW)
        view = UiStateRepo(services.conn).get()
        replay = _stage_text(flow)
        # conversation moved on: the replay must not rewrite or reset it
        assert UiStateRepo(services.conn).get().version == view.version
        assert UiStateRepo(services.conn).get().state == view.state
        assert texts.PICK_TIME in replay.text
        assert view.arg('schedule_picker')
        assert texts.BTN_DAY_SLOTS in [b.label for row in replay.rows for b in row]


class TestDispatcherReplay:
    """End-to-end through the real aiogram Dispatcher: the same raw update
    arrives twice (the crash-and-redelivery pattern) — intake must not apply
    it twice, and Continue must still produce exactly one draft."""

    def test_redelivered_album_item_through_dispatcher(self, services):
        from test_ui_handlers_offline import (
            _Harness,
            _private_text,
            _raw_message,
        )

        h = _Harness(services)
        h.feed(_private_text("/start"))
        h.press_text("Создать пост")
        raw1 = _raw_message(
            message_id=10, media_group_id="grp1",
            photo=[{"file_id": "p1", "file_unique_id": "u1", "width": 800,
                    "height": 600}],
            caption="подпись альбома",
        )
        raw2 = _raw_message(
            message_id=11, media_group_id="grp1",
            video={"file_id": "v1", "file_unique_id": "v1u", "width": 640,
                   "height": 480, "duration": 12},
        )
        h.feed({"message": raw1})
        h.feed({"message": raw2})
        # crash between effect-commit and inbox.complete: Telegram redelivers
        h.feed({"message": raw1})
        assert "Альбом (2)" in h.last_text()  # staged count unchanged
        assert texts.REPLACE_ASK.split("\n")[0] not in h.last_text()
        h.press_text("Продолжить")
        post_ids = services.drafts.list_drafts()
        assert len(post_ids) == 1  # exactly one post after Continue
        content = services.drafts.get_content(post_ids[0])
        assert content.kind == "album"
        assert [i.file_id for i in content.items] == ["p1", "v1"]
        # the first item's caption survived intake and the album build
        assert content.items[0].caption == "подпись альбома"
        assert len(h.preview.owner_previews) == 1

    def test_redelivered_text_through_dispatcher(self, services):
        from test_ui_handlers_offline import (
            _Harness,
            _private_text,
            _raw_message,
        )

        h = _Harness(services)
        h.feed(_private_text("/start"))
        h.press_text("Создать пост")
        raw = _raw_message(message_id=12, text="один и тот же текст")
        h.feed({"message": raw})
        h.feed({"message": raw})
        assert "Текст (19)" in h.last_text()
        assert texts.REPLACE_ASK.split("\n")[0] not in h.last_text()


def _draft_no(text: str) -> int:
    import re

    match = re.search(r"№(\d+)", text)
    assert match, text
    return int(match.group(1))
