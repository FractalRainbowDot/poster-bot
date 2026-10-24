"""Planned-pipeline owner UI: configured-signature intake, frozen plans and
exact stack previews. Synthetic offline fixtures — fake transport only; the
channel publish path must never be touched by previews or intake.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from conftest import NOW
from ui_flow_helpers import build_flow, find_token, labels, press, run

import poster_bot.ui.texts as texts
from poster_bot.domain.content import Content, Entity, MediaRef, utf16_length
from poster_bot.domain.planner import PublicationPlan
from poster_bot.domain.signature import SignatureConfig
from poster_bot.repo.plans import PlanStore
from poster_bot.transport.ports import DefinitiveFailure, Sent

SIG = SignatureConfig(main_text="НашКанал",
                      main_url="https://t.me/our_channel",
                      partner_text="Партнёр",
                      partner_url="https://t.me/partner_one")
SIG_OTHER = SignatureConfig(main_text="ДругойКанал",
                            main_url="https://t.me/other_channel",
                            partner_text="ПартнёрДва",
                            partner_url="https://t.me/partner_two")
OWNER = 555


def planned(services):
    flow, preview = build_flow(services, signature=SIG)
    flow.navigation_store(OWNER)  # mirrors what handlers.py does per update
    return flow, preview


def store(services) -> PlanStore:
    return PlanStore(services.conn)


def draft_no(reply) -> int:
    assert reply.review_post_id is not None
    return reply.review_post_id


def review(flow, reply):
    """Run the presentation phase used by handlers, with fake private I/O."""
    return run(flow.review_reply(reply, now=NOW))


def staged_of(flow):
    return flow._staged(flow._ui())


def lifecycle_ids(flow) -> tuple[list, dict]:
    data = flow._navigation.lifecycle._load()
    return list(data["candidate"]), data["pending"]


PARA_A = "первый абзац " * 30
PARA_B = "второй абзац " * 30


class TestChunkIntake:
    def test_chunks_append_until_continue(self, services):
        flow, preview = planned(services)
        run(flow.new_post(now=NOW))
        r1 = run(flow.text(now=NOW, text=PARA_A, source_key=f"{OWNER}:101"))
        r2 = run(flow.text(now=NOW, text=PARA_B, source_key=f"{OWNER}:102"))
        r3 = run(flow.text(now=NOW, text="финал", source_key=f"{OWNER}:103"))
        assert r2.text != texts.REPLACE_ASK  # append, not a replace duel
        assert r3.text != texts.REPLACE_ASK
        body = staged_of(flow).text
        assert body == f"{PARA_A}\n\n{PARA_B}\n\nфинал"
        assert flow.input_captured(f"{OWNER}:101")
        assert flow.input_captured(f"{OWNER}:102")
        assert flow.input_captured(f"{OWNER}:103")

    def test_third_chunk_never_replaces_pending_second(self, services):
        # an unsupported-media conflict parks the owner in AWAIT_REPLACE;
        # further chunks must ACCUMULATE in the pending candidate
        flow, _ = planned(services)
        run(flow.new_post(now=NOW))
        run(flow.media(now=NOW, kind="photo", file_id="PH1", caption=None,
                       media_group_id="g1", source_key=f"{OWNER}:201"))
        run(flow.media(now=NOW, kind="audio", file_id="AU1", caption=None,
                       media_group_id=None, source_key=f"{OWNER}:202"))
        assert flow._ui().state.value == "await_replace"
        r1 = run(flow.text(now=NOW, text="вторая часть",
                           source_key=f"{OWNER}:203"))
        r2 = run(flow.text(now=NOW, text="третья часть",
                           source_key=f"{OWNER}:204"))
        assert r1.text == texts.REPLACE_ASK
        pending = flow._ui().arg("pending_replace") or {}
        assert "вторая часть" in pending.get("text", "")
        assert "третья часть" in pending.get("text", "")

    def test_redelivered_chunk_does_not_duplicate(self, services):
        flow, _ = planned(services)
        run(flow.new_post(now=NOW))
        run(flow.text(now=NOW, text=PARA_A, source_key=f"{OWNER}:301"))
        replay = run(flow.text(now=NOW, text=PARA_A, source_key=f"{OWNER}:301"))
        assert staged_of(flow).text == PARA_A
        assert replay.text  # idempotent explanation, not a second append


class TestMediaIntake:
    def test_two_media_and_text_chronological_entities_survive(self, services):
        flow, preview = planned(services)
        run(flow.new_post(now=NOW))
        run(flow.media(now=NOW, kind="photo", file_id="PH1",
                       caption="first caption",
                       caption_entities=(Entity(type="bold", offset=0,
                                                length=5),),
                       media_group_id="g1", source_key=f"{OWNER}:401"))
        run(flow.text(now=NOW, text="body text here",
                      entities=(Entity(type="italic", offset=0, length=4),),
                      source_key=f"{OWNER}:402"))
        r3 = run(flow.media(now=NOW, kind="photo", file_id="PH2",
                            caption="second caption",
                            caption_entities=(Entity(type="bold", offset=7,
                                                     length=7),),
                            media_group_id="g2",  # a DIFFERENT source album
                            source_key=f"{OWNER}:403"))
        created = press(flow, find_token(r3, texts.BTN_CONTINUE), now=NOW)
        post_id = draft_no(created)
        stored = store(services).get(post_id)
        logical, plan = stored.logical, stored.plan
        # chronological order: caption 1, body, caption 2
        assert logical.body.index("first caption") < \
            logical.body.index("body text here") < \
            logical.body.index("second caption")
        # entities remap onto the merged body verbatim
        bold_at = logical.body.index("first caption")
        assert any(e.type == "bold" and e.offset == bold_at and e.length == 5
                   for e in logical.entities)
        assert any(e.type == "italic"
                   and e.offset == logical.body.index("body text here")
                   for e in logical.entities)
        assert any(e.type == "bold"
                   and e.offset == logical.body.index("second caption") + 7
                   for e in logical.entities)
        # one ordered album in arrival order, captions folded to the body
        album_parts = [p for p in plan.parts if p.content.kind == "album"]
        assert len(album_parts) == 1
        items = album_parts[0].content.items
        assert [i.file_id for i in items] == ["PH1", "PH2"]
        assert all(i.caption is None for i in items)
        for part in plan.parts:
            assert part.published_text().endswith(SIG.rendered())

    def test_eleventh_album_item_refused_not_captured(self, services):
        flow, preview = planned(services)
        run(flow.new_post(now=NOW))
        for i in range(10):
            run(flow.media(now=NOW, kind="photo", file_id=f"PH{i}",
                           caption=None, media_group_id="g1",
                           source_key=f"{OWNER}:5{i:02d}"))
        overflow = run(flow.media(now=NOW, kind="photo", file_id="PH11",
                                  caption=None, media_group_id="g1",
                                  source_key=f"{OWNER}:599"))
        assert "10" in overflow.text and "не добавлен" in overflow.text
        assert not flow.input_captured(f"{OWNER}:599")  # message NOT deleted
        assert len(staged_of(flow).album_items) == 10
        assert [i.file_id for i in staged_of(flow).album_items][:1] == ["PH0"]
        created = press(flow, find_token(overflow, texts.BTN_CONTINUE),
                        now=NOW)
        stored = store(services).get(draft_no(created))
        album = next(p for p in stored.plan.parts
                     if p.content.kind == "album")
        assert len(album.content.items) == 10
        assert stored.plan.diagnostics == ()  # kept ten, extra never entered

    def test_cross_album_media_combine_into_one_ordered_album(self, services):
        flow, _ = planned(services)
        run(flow.new_post(now=NOW))
        run(flow.media(now=NOW, kind="photo", file_id="A1", caption=None,
                       media_group_id="ga", source_key=f"{OWNER}:601"))
        mid = run(flow.media(now=NOW, kind="video", file_id="B1", caption=None,
                             media_group_id="gb", source_key=f"{OWNER}:602"))
        assert mid.text != texts.REPLACE_ASK
        assert staged_of(flow).kind == "album"
        assert [i.file_id for i in staged_of(flow).album_items] == ["A1", "B1"]
        # a single media then a solo photo also combine
        run(flow.new_post(now=NOW))
        run(flow.media(now=NOW, kind="photo", file_id="S1", caption=None,
                       media_group_id=None, source_key=f"{OWNER}:611"))
        run(flow.media(now=NOW, kind="photo", file_id="S2", caption=None,
                       media_group_id=None, source_key=f"{OWNER}:612"))
        assert staged_of(flow).kind == "album"


class TestContinuePersistence:
    def test_frozen_plan_json_matches_preview_stack_media_ids(self, services):
        flow, preview = planned(services)
        run(flow.new_post(now=NOW))
        run(flow.media(now=NOW, kind="photo", file_id="M1",
                       caption="верхний", media_group_id="g",
                       source_key=f"{OWNER}:701"))
        run(flow.media(now=NOW, kind="photo", file_id="M2", caption=None,
                       media_group_id="g", source_key=f"{OWNER}:702"))
        created = press(flow, find_token(
            run(flow.text(now=NOW, text="тело поста",
                          source_key=f"{OWNER}:703")),
            texts.BTN_CONTINUE), now=NOW)
        post_id = draft_no(created)
        _, complete = review(flow, created)
        assert complete
        stored = store(services).get(post_id)
        # previews ARE the plan: same physical contents, same order
        assert [c.to_json() for c in preview.owner_previews] == \
            [p.content.to_json() for p in stored.plan.parts]
        media_ids = [i.file_id for p in stored.plan.parts
                     for i in (p.content.items or
                               ([p.content.media] if p.content.media else []))]
        assert "M1" in media_ids and "M2" in media_ids
        # draft content row equals the plan's FIRST physical part
        assert services.drafts.get_content(post_id).to_json() == \
            stored.plan.parts[0].content.to_json()
        # every preview message id of the whole stack is tracked
        candidate, _pending = lifecycle_ids(flow)
        assert len(candidate) == len(preview.owner_previews) + \
            sum(len(p.content.items) - 1 for p in stored.plan.parts
                if p.content.kind == "album")

    def test_manual_split_retains_everything(self, services):
        flow, preview = planned(services)
        run(flow.new_post(now=NOW))
        monster = "неделимый " * 900  # one paragraph, way over any budget
        run(flow.text(now=NOW, text=monster, source_key=f"{OWNER}:801"))
        r2 = run(flow.text(now=NOW, text="ещё", source_key=f"{OWNER}:802"))
        reply = press(flow, find_token(r2, texts.BTN_CONTINUE), now=NOW)
        assert "разб" in reply.text  # asks the owner to split manually
        assert "Длины абзацев" in reply.text
        assert monster[:40] not in reply.text  # lengths only, never raw text
        assert staged_of(flow).text.startswith(monster[:20])
        assert "ещё" in staged_of(flow).text
        assert services.drafts.list_drafts() == []  # nothing half-created
        assert preview.owner_previews == []  # no preview of a broken stack


class TestForwardConsent:
    def test_copy_prompt_before_conversion_and_decline_keeps_source(self,
                                                                    services):
        flow, _ = planned(services)
        run(flow.new_post(now=NOW))
        card = run(flow.forwarded(
            now=NOW, src_chat_id=OWNER, src_message_id=201,
            media_group_id=None,
            copy_candidate=_copy_candidate(), source_key=f"{OWNER}:901"))
        assert card.text == texts.FORWARD_PLANNED_ASK
        labels = {b.label for row in card.rows for b in row}
        assert texts.BTN_COPY_SIGNED in labels
        assert texts.BTN_FORWARD_AS_IS in labels
        # nothing converted before the explicit press
        assert staged_of(flow).kind == "forward"
        assert staged_of(flow).forward_ids == [201]
        # declining the copy (pressing nothing / leaving the card later)
        # must not have released any source yet: the release is part of the
        # accepted-copy reply only
        assert card.released_inputs == ()

    def test_accept_captures_copy_entities_first_then_release(self, services):
        flow, _ = planned(services)
        run(flow.new_post(now=NOW))
        card = run(flow.forwarded(
            now=NOW, src_chat_id=OWNER, src_message_id=201,
            media_group_id=None,
            copy_candidate=_copy_candidate(), source_key=f"{OWNER}:911"))
        accepted = press(flow, find_token(card, "Создать копию"), now=NOW)
        assert accepted.released_inputs == ((OWNER, 201),)
        # the copy's file_id/entities were captured into staged BEFORE the
        # release was offered at all
        staged = staged_of(flow)
        assert staged.kind == "media"
        assert staged.media is not None
        assert staged.media.file_id == "COPYPH1"
        # Consent folds captions into the lossless logical body, not an
        # outbound MediaRef caption (which has a stricter physical budget).
        assert staged.media.caption is None
        assert staged.text == "исходная подпись"
        assert any(e.type == "bold" for e in staged.entities)


class TestEditPolicy:
    def test_draft_edit_replaces_plan_pair(self, services):
        flow, preview = planned(services)
        run(flow.new_post(now=NOW))
        r = run(flow.text(now=NOW, text="старое содержимое поста",
                          source_key=f"{OWNER}:1001"))
        created = press(flow, find_token(r, texts.BTN_CONTINUE), now=NOW)
        post_id = draft_no(created)
        old_plan = store(services).get(post_id).plan.to_json()
        card = run(flow.draft_preview(now=NOW, post_id=post_id))
        modes = press(flow, find_token(card, texts.BTN_EDIT_CONTENT), now=NOW)
        press(flow, find_token(modes, texts.BTN_EDIT_TEXT), now=NOW)
        r2 = run(flow.text(now=NOW, text="новое содержимое",
                           source_key=f"{OWNER}:1002"))
        from ui_flow_helpers import present_reply
        r2 = run(present_reply(flow, r2, now=NOW, owner_id=OWNER))
        confirm = press(flow, find_token(r2, texts.BTN_CONTINUE), now=NOW)
        assert store(services).get(post_id).plan.to_json() == old_plan
        done = press(flow, find_token(confirm, texts.BTN_CONFIRM), now=NOW)
        assert texts.CONTENT_UPDATED in done.text
        stored = store(services).get(post_id)
        assert stored.plan.to_json() != old_plan  # pair replaced, not appended
        assert "новое содержимое" in stored.logical.body
        assert "старое" not in stored.logical.body
        assert services.drafts.get_content(post_id).to_json() == \
            stored.plan.parts[0].content.to_json()

    def test_scheduled_edit_flows_through_edit_content(self, services):
        flow, preview = planned(services)
        run(flow.new_post(now=NOW))
        r = run(flow.text(now=NOW, text="запланировано старое",
                          source_key=f"{OWNER}:1101"))
        created = press(flow, find_token(r, texts.BTN_CONTINUE), now=NOW)
        post_id = draft_no(created)
        confirm = press(flow, find_token(created, "12:00"), now=NOW)
        scheduled = press(flow, find_token(confirm, texts.BTN_CONFIRM),
                          now=NOW)
        pub = services.publications.get_by_post(post_id)
        assert pub.status.value == "scheduled"
        old_plan = store(services).get(post_id).plan.to_json()
        edit = press(flow, find_token(scheduled,
                                      texts.BTN_EDIT_CONTENT), now=NOW)
        press(flow, find_token(edit, texts.BTN_EDIT_TEXT), now=NOW)
        r2 = run(flow.text(now=NOW, text="запланировано новое",
                           source_key=f"{OWNER}:1102"))
        from ui_flow_helpers import present_reply
        r2 = run(present_reply(flow, r2, now=NOW, owner_id=OWNER))
        confirm = press(flow, find_token(r2, texts.BTN_CONTINUE), now=NOW)
        assert services.publications.get(pub.id) == pub
        assert store(services).get(post_id).plan.to_json() == old_plan
        done = press(flow, find_token(confirm, texts.BTN_CONFIRM), now=NOW)
        _, complete = review(flow, done)
        assert complete
        assert texts.EDIT_PAUSED_NOTE in done.text
        pub = services.publications.get(pub.id)
        assert pub.status.value == "suspended"  # existing pause contract
        stored = store(services).get(post_id)
        assert stored.plan.to_json() != old_plan
        assert "новое" in stored.logical.body
        assert services.drafts.get_content(post_id).to_json() == \
            stored.plan.parts[0].content.to_json()
        # owner sees the NEW frozen stack, not the old preview
        assert preview.owner_previews[-1].to_json() == \
            stored.plan.parts[0].content.to_json()


class TestFrozenPlanIntegrity:
    def test_reopen_uses_stored_plan_env_change_never_mutates(self, services):
        flow, preview = planned(services)
        run(flow.new_post(now=NOW))
        r = run(flow.text(now=NOW, text="замороженный план",
                          source_key=f"{OWNER}:1201"))
        created = press(flow, find_token(r, texts.BTN_CONTINUE), now=NOW)
        post_id = draft_no(created)
        before = store(services).get(post_id).plan.to_json()
        # "env changed": a NEW flow composed with a different signature
        flow2, preview2 = build_flow(services, signature=SIG_OTHER)
        flow2.navigation_store(OWNER)
        view = run(flow2.draft_preview(now=NOW, post_id=post_id))
        _, complete = review(flow2, view)
        assert complete
        # the preview is byte-identical to the OLD stored plan, not the new
        # environment's rendering
        assert preview2.owner_previews[-1].to_json() == json_part0(before)
        assert store(services).get(post_id).plan.to_json() == before

    def test_legacy_draft_preview_never_silently_normalizes_unsigned_content(self,
                                                                          services):
        # a draft created BEFORE the signature was configured (legacy flow)
        legacy = build_flow(services)[0]
        run(legacy.new_post(now=NOW))
        r = run(legacy.media(now=NOW, kind="photo", file_id="OLD1",
                             caption="старое фото", source_key=f"{OWNER}:1301"))
        created = press(legacy, find_token(r, texts.BTN_CONTINUE), now=NOW)
        post_id = draft_no(created)
        original = services.drafts.get_content(post_id).to_json()
        assert store(services).has_plan(post_id) is False
        flow, preview2 = planned(services)
        view = run(flow.draft_preview(now=NOW, post_id=post_id))
        view, complete = review(flow, view)
        assert not complete
        assert texts.BTN_CONFIRM not in labels(view)
        assert preview2.owner_previews == []  # no unsigned or ambient-signed preview
        assert not store(services).has_plan(post_id)
        assert services.drafts.get_content(post_id).to_json() == original
        # Reopening cannot silently turn a legacy draft into a signed pair.
        again = run(flow.draft_preview(now=NOW, post_id=post_id))
        _, complete = review(flow, again)
        assert not complete
        assert not store(services).has_plan(post_id)
        assert services.drafts.get_content(post_id).to_json() == original
        assert preview2.owner_previews == [] and preview2.calls == []


class TestPreviewStackLifecycle:
    def test_leave_retires_whole_stack(self, services):
        flow, preview = planned(services)
        run(flow.new_post(now=NOW))
        r = run(flow.text(now=NOW, text=PARA_A, source_key=f"{OWNER}:1401"))
        r2 = run(flow.text(now=NOW, text=PARA_B, source_key=f"{OWNER}:1402"))
        created = press(flow, find_token(r2, texts.BTN_CONTINUE), now=NOW)
        _, complete = review(flow, created)
        assert complete
        candidate, _ = lifecycle_ids(flow)
        assert len(candidate) >= 1
        # owner leaves the draft card: navigation to another screen mirrors
        # handlers.py: presented(keep_preview=flow.preview_context())
        run(flow.menu_navigation(label=texts.MENU_DRAFTS, now=NOW))
        flow._navigation.lifecycle.presented(
            keep_preview=flow.preview_context(), now=NOW)
        candidate_after, pending_after = lifecycle_ids(flow)
        assert candidate_after == []
        assert all(str(mid) in pending_after for mid in candidate)

    def test_partial_preview_failure_tracks_ids_and_leaks_nothing(self,
                                                                  services):
        flow, preview = planned(services)
        preview.preview_script = [Sent(message_ids=(8_111_111,),
                                       permalink=""),
                                  DefinitiveFailure("api_error")]
        run(flow.new_post(now=NOW))
        # four big paragraphs force a two-part plan so the stack can fail
        # halfway: part 1 acked, part 2 refused by the API
        para = "абзац " * 340  # ~2040 chars
        for i in range(4):
            run(flow.text(now=NOW, text=para,
                          source_key=f"{OWNER}:15{i:02d}"))
        r5 = run(flow.text(now=NOW, text="финал",
                           source_key=f"{OWNER}:1599"))
        created = press(flow, find_token(r5, texts.BTN_CONTINUE), now=NOW)
        created, complete = review(flow, created)
        assert not complete
        assert texts.DRAFT_SAVED in created.text  # post durable despite failure
        assert 'Не удалось показать весь пост' in created.text
        assert 'Повторить предпросмотр' in labels(created)
        assert preview.calls == []  # nothing public, channel untouched
        candidate, pending = lifecycle_ids(flow)
        assert candidate == []  # an incomplete stack must never be promoted
        assert str(8_111_111) in pending  # acknowledged part is owed cleanup
        assert flow._navigation.lifecycle._load()['active'] == []
        assert services.drafts.is_draft(draft_no(created))


class TestAtomicDraftPlanCreation:
    """posts + post_plans land in ONE immediate transaction: a crash in the
    creation window must leave no half-created draft (a planless draft's
    content is the SIGNED first part — its unsigned body is unrecoverable).
    """

    def test_crash_during_creation_leaves_no_partial_draft(self, services):
        flow, preview = planned(services)
        run(flow.new_post(now=NOW))
        r = run(flow.text(now=NOW, text="важное содержимое",
                          source_key=f"{OWNER}:1601"))
        token = find_token(r, texts.BTN_CONTINUE)
        state_before = flow._ui().state.value
        # a process dying right AFTER the plan row write: the INSERT itself aborts
        services.conn.execute(
            "CREATE TRIGGER crash_after_plan_insert AFTER INSERT ON post_plans"
            " BEGIN SELECT RAISE(ABORT, 'simulated crash after plan insert'); END")
        try:
            # RAISE(ABORT) surfaces as a sqlite3.Error subclass on this driver;
            # immediate_tx must roll the whole transaction back and re-raise it
            with pytest.raises(sqlite3.Error):
                press(flow, token, now=NOW)
            # full rollback: neither row survived, nothing half-known
            assert services.conn.execute(
                "SELECT COUNT(*) FROM posts").fetchone()[0] == 0
            assert services.conn.execute(
                "SELECT COUNT(*) FROM post_plans").fetchone()[0] == 0
            assert services.conn.execute(
                "SELECT COUNT(*) FROM journal_events"
                " WHERE kind='draft_created'").fetchone()[0] == 0
            assert preview.owner_previews == []  # no preview of a phantom post
            assert flow._ui().arg("post_id") is None  # UI remembers no half-post
            assert flow._ui().state.value == state_before
            # the staged payload still holds everything the owner sent
            assert staged_of(flow).text == "важное содержимое"
        finally:
            services.conn.execute("DROP TRIGGER crash_after_plan_insert")
        # the owner can simply continue: a fresh chunk + Continue completes
        r2 = run(flow.text(now=NOW, text="ещё часть",
                           source_key=f"{OWNER}:1602"))
        created = press(flow, find_token(r2, texts.BTN_CONTINUE), now=NOW)
        post_id = draft_no(created)
        assert services.drafts.is_draft(post_id)
        stored = store(services).get(post_id)
        assert stored is not None and stored.plan.parts  # pair, not planless
        assert "важное содержимое" in stored.logical.body
        assert "ещё часть" in stored.logical.body


class TestVerbatimIntake:
    """What the owner sent is what is planned: surrounding whitespace and
    UTF-16 entity offsets survive intake, folding and normalization intact."""

    def test_chunk_whitespace_and_entities_survive_to_the_frozen_plan(self,
                                                                      services):
        flow, _ = planned(services)
        run(flow.new_post(now=NOW))
        chunk = "  привет, мир  "  # the spaces ARE owner text
        run(flow.text(now=NOW, text=chunk,
                      entities=(Entity(type="bold", offset=2, length=11),),
                      source_key=f"{OWNER}:1701"))
        r2 = run(flow.text(now=NOW, text="вторая  ",
                           source_key=f"{OWNER}:1702"))
        created = press(flow, find_token(r2, texts.BTN_CONTINUE), now=NOW)
        stored = store(services).get(draft_no(created))
        assert stored.logical.body == f"{chunk}\n\nвторая  "
        assert any(e.type == "bold" and e.offset == 2 and e.length == 11
                   for e in stored.logical.entities)
        # plan slices reconstruct the body character-for-character
        assert stored.plan.reconstructed_body() == stored.logical.body

    def test_caption_with_surrounding_whitespace_folds_verbatim(self,
                                                                services):
        flow, _ = planned(services)
        run(flow.new_post(now=NOW))
        run(flow.media(now=NOW, kind="photo", file_id="W1",
                       caption="  подпись  ",
                       caption_entities=(Entity(type="italic", offset=2,
                                                length=7),),
                       media_group_id=None, source_key=f"{OWNER}:1711"))
        r2 = run(flow.text(now=NOW, text="тело",
                           source_key=f"{OWNER}:1712"))
        created = press(flow, find_token(r2, texts.BTN_CONTINUE), now=NOW)
        stored = store(services).get(draft_no(created))
        assert stored.logical.body == "  подпись  \n\nтело"
        assert any(e.type == "italic" and e.offset == 2 and e.length == 7
                   for e in stored.logical.entities)
        assert stored.plan.reconstructed_body() == stored.logical.body
        # a blank-LOOKING caption is still owner text: presence is a
        # None/empty check, never a stripped comparison (the old strip silently
        # dropped it, so the photo's owner-visible caption vanished from plan)
        run(flow.new_post(now=NOW))
        run(flow.media(now=NOW, kind="photo", file_id="W2", caption=" ",
                       caption_entities=(), media_group_id=None,
                       source_key=f"{OWNER}:1713"))
        r3 = run(flow.text(now=NOW, text="вторая",
                           source_key=f"{OWNER}:1714"))
        created2 = press(flow, find_token(r3, texts.BTN_CONTINUE), now=NOW)
        stored2 = store(services).get(draft_no(created2))
        assert stored2.logical.body == " \n\nвторая"
        assert stored2.plan.reconstructed_body() == stored2.logical.body

    def test_normalize_content_preserves_whitespace_and_utf16_offsets(self):
        from poster_bot.ui import intake
        # legacy text-only draft: the old body-strip silently invalidated
        # every entity of a leading-whitespace text
        text_only = Content(kind="text", text="  привет  ",
                            entities=(Entity(type="bold", offset=2,
                                             length=6),))
        logical = intake.normalize_content(text_only)
        assert logical.body == "  привет  "
        assert logical.entities[0].offset == 2

        # legacy album: per-item captions fold verbatim in stored order,
        # offsets remap onto the merged body in UTF-16 units
        album = Content(
            kind="album",
            items=(MediaRef(kind="photo", file_id="P1",
                            caption="  альбом  ",
                            caption_entities=(Entity(type="bold", offset=2,
                                                     length=6),)),
                   MediaRef(kind="photo", file_id="P2",
                            caption=" 🎉 срез  ",
                            caption_entities=(Entity(type="bold", offset=1,
                                                     length=2),)),))
        merged = intake.normalize_content(album)
        assert merged.body == "  альбом  \n\n 🎉 срез  "  # verbatim, not stripped
        bolds = [e for e in merged.entities if e.type == "bold"]
        assert len(bolds) == 2
        assert any(e.offset == 2 and e.length == 6 for e in bolds)
        emoji_at = merged.body.index("🎉")
        assert any(e.offset == utf16_length(merged.body[:emoji_at])
                   and e.length == 2 for e in bolds)


class TestPlannedRaceHardening:
    """Availability hardening: a concurrent step must never leak a raw domain
    error into the update handler. A lost conversation compare-and-set or a
    plan freeze that loses to the sender's claim answers with the EXISTING
    safe reply (intake replay / op-failed card): the incoming message stays
    uncaptured and visible, durable state is untouched, and no preview or
    channel call repeats a flow effect (no infinite pending exception loop)."""

    def _bump_hook(self, flow, services):
        """One-time controlled race: the FIRST compare-and-set loses because a
        concurrent step lands between the flow's entry read and its durable
        write (the real CAS then raises the real StaleStateError)."""
        repo = flow._state
        real_set = repo.set
        fired: list[int] = []

        def racy_set(*args, **kw):
            if kw.get("expected_version") is not None and not fired:
                fired.append(1)
                row = services.conn.execute(
                    "SELECT value FROM ui_state WHERE key='conversation'"
                ).fetchone()
                data = json.loads(row["value"])
                data["version"] += 1
                services.conn.execute(
                    "UPDATE ui_state SET value=? WHERE key='conversation'",
                    (json.dumps(data, ensure_ascii=False, sort_keys=True),),
                )
            return real_set(*args, **kw)

        repo.set = racy_set

    def test_text_chunk_lost_cas_is_safe_and_converges(self, services):
        flow, preview = planned(services)
        run(flow.new_post(now=NOW))
        version_before = flow._ui().version
        self._bump_hook(flow, services)
        key = f"{OWNER}:2001"
        reply = run(flow.text(now=NOW, text=PARA_A, source_key=key))
        assert reply.text  # safe replay answer, no raw exception
        assert not staged_of(flow).text  # nothing half-applied (empty staged)
        assert not flow.input_captured(key)  # incoming message NOT deleted
        # only the concurrent bump survived: the failed CAS added no version
        assert flow._ui().version == version_before + 1
        assert services.drafts.list_drafts() == []  # no wrong post
        assert preview.calls == [] and preview.owner_previews == []
        candidate, _pending = lifecycle_ids(flow)
        assert candidate == []  # no preview/card side effects either
        # inbox redelivery converges: the still-visible message lands ONCE
        again = run(flow.text(now=NOW, text=PARA_A, source_key=key))
        assert again.text
        assert staged_of(flow).text == PARA_A  # exactly one append, no dupes
        assert flow.input_captured(key)  # now released — no pending loop

    def test_media_chunk_lost_cas_is_safe_and_converges(self, services):
        flow, preview = planned(services)
        run(flow.new_post(now=NOW))
        self._bump_hook(flow, services)
        key = f"{OWNER}:2101"
        reply = run(flow.media(now=NOW, kind="photo", file_id="PH9",
                               caption=None, media_group_id=None,
                               source_key=key))
        assert reply.text
        assert staged_of(flow).kind is None  # durable staged untouched
        assert staged_of(flow).album_items == []  # not half-absorbed anywhere
        assert not flow.input_captured(key)  # message NOT deleted
        assert services.drafts.list_drafts() == []
        assert preview.calls == [] and preview.owner_previews == []
        again = run(flow.media(now=NOW, kind="photo", file_id="PH9",
                               caption=None, media_group_id=None,
                               source_key=key))
        assert again.text
        # a solo photo stages as a one-item planned album — exactly once
        assert [i.file_id for i in staged_of(flow).album_items] == ["PH9"]
        assert flow.input_captured(key)

    def test_redelivered_media_replays_without_error(self, services):
        # redelivery INTO an intake state is deduplicated by the durable
        # effect: the planned media path must replay idempotently instead of
        # raising EffectAlreadyApplied (or offering a false replace duel).
        flow, _ = planned(services)
        run(flow.new_post(now=NOW))
        key = f"{OWNER}:2201"
        run(flow.media(now=NOW, kind="photo", file_id="PH9", caption=None,
                       media_group_id=None, source_key=key))
        assert flow.input_captured(key)
        replay = run(flow.media(now=NOW, kind="photo", file_id="PH9",
                                caption=None, media_group_id=None,
                                source_key=key))
        assert replay.text and replay.text != texts.REPLACE_ASK
        assert flow._ui().state.value == "creating"  # no bogus state change
        # exactly one item survived: the replayed append rolled back with the
        # rejected effect marker, the committed solo album is untouched
        assert [i.file_id for i in staged_of(flow).album_items] == ["PH9"]

    def test_claimed_legacy_pub_preview_never_freezes_or_sends(self, services):
        # Presentation no longer normalizes legacy content. A claimed legacy
        # publication stays immutable, without any ambient-signature freeze.
        legacy = build_flow(services)[0]
        run(legacy.new_post(now=NOW))
        r = run(legacy.text(now=NOW, text="старое тело поста",
                            source_key=f"{OWNER}:2301"))
        created = press(legacy, find_token(r, texts.BTN_CONTINUE), now=NOW)
        post_id = draft_no(created)
        confirm = press(legacy, find_token(created, "12:00"), now=NOW)
        press(legacy, find_token(confirm, texts.BTN_CONFIRM), now=NOW)
        pub = services.publications.get_by_post(post_id)
        assert pub.status.value == "scheduled"
        assert store(services).has_plan(post_id) is False

        flow, preview = planned(services)
        def forbidden_save(*args, **kwargs):
            pytest.fail('presentation must never freeze a legacy plan')

        flow._plans.save = forbidden_save
        services.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch)
        claimed = services.publications.get(pub.id)
        original = services.drafts.get_content(post_id).to_json()
        view = run(flow.pub_preview(now=NOW, pub_id=pub.id))  # must not raise
        view, complete = review(flow, view)
        assert texts.PREVIEW_SENT not in view.text
        assert "идёт отправка" in view.text  # the CURRENT truthful card
        assert preview.owner_previews == []  # no preview of the racing stack
        assert preview.calls == []  # the channel was never touched
        assert store(services).has_plan(post_id) is False  # nothing persisted
        status = services.conn.execute(
            "SELECT status FROM publications WHERE id=?", (pub.id,)
        ).fetchone()["status"]
        assert status == "sending"  # the concurrent claim stays intact
        assert services.publications.get(pub.id) == claimed
        assert services.drafts.get_content(post_id).to_json() == original


def draft_of(flow) -> int:
    return int(flow._ui().arg("post_id"))


def json_part0(plan_json: str) -> str:
    import json
    data = json.loads(plan_json)
    return data["parts"][0]["content_json"]


def _copy_candidate():
    from poster_bot.ui.staged import copy_candidate_from_media
    return copy_candidate_from_media(
        "photo", "COPYPH1", "исходная подпись",
        (Entity(type="bold", offset=0, length=7),), None)
