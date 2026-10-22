"""Owner-flow content collection: text/media/album/forward intake with
explicit replace/caption/forward-copy decisions and restart durability.
Fully offline: fake transport only, channel publish must never be called."""
from __future__ import annotations

import re
import pytest
from conftest import NOW
from ui_flow_helpers import build_flow, find_token, labels, press, press_label, reopen, run

import poster_bot.ui.texts as texts


def _draft_number(reply_text: str) -> int:
    match = re.search(r"№(\d+)", reply_text)
    assert match, reply_text
    return int(match.group(1))


class TestCollectionBasics:
    def test_new_post_asks_content(self, services):
        flow, preview = build_flow(services)
        reply = run(flow.new_post(now=NOW))
        assert reply.text == texts.ASK_CONTENT
        assert texts.BTN_CONTINUE in labels(reply)
        assert preview.calls == [] and preview.owner_previews == []

    def test_text_stages_then_continue_creates_draft_with_preview(self, services):
        flow, preview = build_flow(services)
        run(flow.new_post(now=NOW))
        staged = run(flow.text(now=NOW, text="Привет, мир"))
        assert "Текст (11)" in staged.text
        created = press(flow, find_token(staged, texts.BTN_CONTINUE), now=NOW)
        assert texts.DRAFT_SAVED in created.text
        assert texts.PREVIEW_SENT in created.text
        post_id = _draft_number(created.text)
        content = services.drafts.get_content(post_id)
        assert content.kind == "text" and content.text == "Привет, мир"
        # preview went to the OWNER path; the channel was never published to
        assert len(preview.owner_previews) == 1
        assert preview.calls == []

    def test_continue_without_content_is_refused(self, services):
        flow, _ = build_flow(services)
        reply = run(flow.new_post(now=NOW))
        gone = press(flow, find_token(reply, texts.BTN_CONTINUE), now=NOW)
        assert gone.text == texts.NOTHING_STAGED
        assert services.drafts.list_drafts() == []

    def test_unsupported_content_explains(self, services):
        flow, _ = build_flow(services)
        run(flow.new_post(now=NOW))
        reply = flow.unsupported(now=NOW)
        assert reply.text == texts.UNSUPPORTED


class TestExplicitReplace:
    def _two_texts(self, services):
        flow, _ = build_flow(services)
        run(flow.new_post(now=NOW))
        run(flow.text(now=NOW, text="первый"))
        ask = run(flow.text(now=NOW, text="второй вариант"))
        return flow, ask

    def test_second_text_never_silently_overwrites(self, services):
        flow, ask = self._two_texts(services)
        assert ask.text == texts.REPLACE_ASK
        assert texts.BTN_REPLACE in labels(ask)
        assert texts.BTN_KEEP in labels(ask)

    def test_keep_restores_previous_text(self, services):
        flow, ask = self._two_texts(services)
        kept = press_label(flow, ask, texts.BTN_KEEP, now=NOW)
        assert "Текст (6)" in kept.text  # len("первый") == 6

    def test_replace_yes_swaps_content(self, services):
        flow, ask = self._two_texts(services)
        swapped = press_label(flow, ask, texts.BTN_REPLACE, now=NOW)
        assert "Текст (14)" in swapped.text  # len("второй вариант") == 14

    def test_more_text_while_awaiting_updates_candidate(self, services):
        flow, ask = self._two_texts(services)
        updated = run(flow.text(now=NOW, text="третий"))
        assert updated.text == texts.REPLACE_ASK
        swapped = press_label(flow, updated, texts.BTN_REPLACE, now=NOW)
        assert "Текст (6)" in swapped.text  # len("третий") == 6


class TestCaption:
    def test_fresh_text_becomes_caption_only_via_explicit_button(self, services):
        flow, _ = build_flow(services)
        run(flow.new_post(now=NOW))
        run(flow.media(now=NOW, kind="photo", file_id="ph1",
                       caption=None, media_group_id=None))
        ask = run(flow.text(now=NOW, text=" подпись "))
        assert ask.text == texts.CAPTION_ASK
        assert texts.BTN_AS_CAPTION in labels(ask)
        done = press_label(flow, ask, texts.BTN_AS_CAPTION, now=NOW)
        assert "Фото" in done.text
        created = press_label(flow, done, texts.BTN_CONTINUE, now=NOW)
        content = services.drafts.get_content(_draft_number(created.text))
        assert content.kind == "photo"
        # entities/caption are carried verbatim — nothing silently rewrites
        assert content.media.caption == " подпись "


class TestAlbum:
    def _album(self, services):
        flow, _ = build_flow(services)
        run(flow.new_post(now=NOW))
        run(flow.media(now=NOW, kind="photo", file_id="p1",
                       caption=None, media_group_id="g1"))
        run(flow.media(now=NOW, kind="video", file_id="v1",
                       caption=None, media_group_id="g1"))
        ignored = run(flow.media(now=NOW, kind="audio", file_id="a1",
                                 caption=None, media_group_id="g1"))
        return flow, ignored

    def test_album_buffers_group_and_ignores_bad_items(self, services):
        flow, ignored = self._album(services)
        assert "пропущен" in ignored.text.lower()
        third = run(flow.media(now=NOW, kind="photo", file_id="p2",
                               caption=None, media_group_id="g1"))
        assert "Альбом (3)" in third.text
        created = press_label(flow, third, texts.BTN_CONTINUE, now=NOW)
        content = services.drafts.get_content(_draft_number(created.text))
        assert content.kind == "album"
        assert [item.file_id for item in content.items] == ["p1", "v1", "p2"]

    def test_foreign_group_conflicts_into_replace(self, services):
        flow, _ = self._album(services)
        ask = run(flow.media(now=NOW, kind="photo", file_id="x",
                             caption=None, media_group_id="g2"))
        assert ask.text == texts.REPLACE_ASK
        kept = press_label(flow, ask, texts.BTN_KEEP, now=NOW)
        assert "Альбом (2)" in kept.text

    def test_partial_album_survives_restart(self, services, tmp_path):
        flow2, _ = build_flow(services)
        run(flow2.new_post(now=NOW))
        run(flow2.media(now=NOW, kind="photo", file_id="p1",
                        caption=None, media_group_id="g1"))
        second = run(flow2.media(now=NOW, kind="photo", file_id="p2",
                                 caption=None, media_group_id="g1"))
        token = find_token(second, texts.BTN_CONTINUE)
        # restart: brand-new services over the reopened database
        restarted_services, flow3, _ = reopen(services, tmp_path)
        created = press(flow3, token, now=NOW + 60)
        assert texts.DRAFT_SAVED in created.text
        content = restarted_services.drafts.get_content(_draft_number(created.text))
        assert content.kind == "album"
        assert [item.file_id for item in content.items] == ["p1", "p2"]


    def test_album_per_item_captions_survive_intake_and_restart(
        self, services, tmp_path
    ):
        from poster_bot.domain.content import Entity

        flow, _ = build_flow(services)
        run(flow.new_post(now=NOW))
        run(flow.media(now=NOW, kind="photo", file_id="p1", caption="первая",
                       caption_entities=(Entity("bold", 0, 6),),
                       media_group_id="g1"))
        third = run(flow.media(now=NOW, kind="video", file_id="v1",
                               caption="вторая", media_group_id="g1"))
        assert "Альбом (2)" in third.text
        token = find_token(third, texts.BTN_CONTINUE)
        restarted, flow2, _ = reopen(services, tmp_path)
        created = press(flow2, token, now=NOW + 60)
        content = restarted.drafts.get_content(_draft_number(created.text))
        assert content.kind == "album"
        # every item keeps its own caption+entities verbatim through the
        # durable JSON roundtrip: nothing silently merges into one caption
        assert content.items[0].caption == "первая"
        assert content.items[0].caption_entities == (Entity("bold", 0, 6),)
        assert content.items[1].caption == "вторая"
        assert content.caption is None

    def test_album_caption_button_allowed_when_items_uncaptioned(self, services):
        flow, _ = build_flow(services)
        run(flow.new_post(now=NOW))
        run(flow.media(now=NOW, kind="photo", file_id="p1", caption=None,
                       media_group_id="g1"))
        run(flow.media(now=NOW, kind="video", file_id="v1", caption=None,
                       media_group_id="g1"))
        ask = run(flow.text(now=NOW, text="общая подпись"))
        assert ask.text == texts.CAPTION_ASK
        done = press_label(flow, ask, texts.BTN_AS_CAPTION, now=NOW)
        created = press_label(flow, done, texts.BTN_CONTINUE, now=NOW)
        content = services.drafts.get_content(_draft_number(created.text))
        assert content.caption == "общая подпись"

    def test_album_caption_button_refused_over_item_captions(self, services):
        from poster_bot.ui.flow import CAPTION_AMBIGUOUS_RU

        flow, _ = build_flow(services)
        run(flow.new_post(now=NOW))
        run(flow.media(now=NOW, kind="photo", file_id="p1", caption="первая",
                       media_group_id="g1"))
        run(flow.media(now=NOW, kind="video", file_id="v1", caption="вторая",
                       media_group_id="g1"))
        ask = run(flow.text(now=NOW, text="общая подпись"))
        assert ask.text == texts.CAPTION_ASK
        refused = press_label(flow, ask, texts.BTN_AS_CAPTION, now=NOW)
        # ambiguous action: refused with an explanation, not a silent overwrite
        assert refused.text == CAPTION_AMBIGUOUS_RU
        assert texts.BTN_KEEP in labels(refused)
        kept = press_label(flow, refused, texts.BTN_KEEP, now=NOW)
        created = press_label(flow, kept, texts.BTN_CONTINUE, now=NOW)
        content = services.drafts.get_content(_draft_number(created.text))
        assert content.caption is None
        assert [i.caption for i in content.items] == ["первая", "вторая"]

    def test_forward_copy_album_keeps_every_item_caption(self, services):
        from poster_bot.ui.staged import Staged

        flow, _ = build_flow(services)
        run(flow.new_post(now=NOW))
        candidate = Staged()
        candidate.add_media("photo", "p1", "cap1", (), "g1")
        ask = run(flow.forwarded(now=NOW, src_chat_id=-100999,
                                 src_message_id=42, media_group_id="g1",
                                 copy_candidate=candidate))
        second = Staged()
        second.add_media("video", "v1", "cap2", (), "g1")
        ask = run(flow.forwarded(now=NOW, src_chat_id=-100999,
                                 src_message_id=43, media_group_id="g1",
                                 copy_candidate=second))
        assert ask.text == texts.FORWARD_ASK
        copied = press_label(flow, ask, texts.BTN_MAKE_COPY, now=NOW)
        created = press_label(flow, copied, texts.BTN_CONTINUE, now=NOW)
        content = services.drafts.get_content(_draft_number(created.text))
        assert content.kind == "album"
        assert [i.caption for i in content.items] == ["cap1", "cap2"]


class TestForward:
    def _forward(self, services):
        flow, preview = build_flow(services)
        run(flow.new_post(now=NOW))
        ask = run(flow.forwarded(now=NOW, src_chat_id=-100999,
                                 src_message_id=42, media_group_id=None,
                                 copy_candidate=_photo_candidate()))
        return flow, preview, ask

    def test_forward_defaults_to_as_is_and_creates_source_draft(self, services):
        flow, preview, ask = self._forward(services)
        assert ask.text == texts.FORWARD_ASK
        assert texts.BTN_FORWARD_AS_IS in labels(ask)
        assert texts.BTN_MAKE_COPY in labels(ask)
        # explicit as-is press finalizes: draft of the forward source + preview
        created = press_label(flow, ask, texts.BTN_FORWARD_AS_IS, now=NOW)
        assert texts.DRAFT_SAVED in created.text
        content = services.drafts.get_content(_draft_number(created.text))
        assert content.kind == "forward" and content.mode == "forward"
        assert content.forward.chat_id == -100999
        assert content.forward.message_ids == (42,)
        # the preview shows the source, not a locally re-created post
        assert preview.owner_previews[0].kind == "forward"
        assert preview.calls == []  # channel never touched

    def test_make_copy_collapses_to_editable_parts(self, services):
        flow, _, ask = self._forward(services)
        copied = press_label(flow, ask, texts.BTN_MAKE_COPY, now=NOW)
        assert copied.text.startswith(texts.COPY_READY)
        assert "Фото" in copied.text


def _photo_candidate():
    from poster_bot.ui.staged import copy_candidate_from_media

    return copy_candidate_from_media("photo", "ph-src", None, (), None)
