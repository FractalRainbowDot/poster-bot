"""Legacy planless COPY auto-prepare on the gated sender (production-blocker fix).

The mandatory-signature upgrade must keep OLD scheduled copies working: with a
configured signature the gated sender freezes a missing plan BEFORE the claim
using the identical pure lossless normalizer the preview runs — one atomic
pair-only write; the posts row, ids, settings, schedule, version and provenance
stay byte-identical, and the pair's EMBEDDED signature survives any later
ambient config change. What cannot be prepared (forwards, paragraphs the
mandatory footer cannot fit) is fail-closed: no channel call, nothing written,
schedule intact, owner warned once per process with ids + machine codes only.
The migration readiness scan is the offline READ-ONLY pre-cutover check the
operator runs before switching images.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random

import pytest
from conftest import NOW, Instant, text_content
from fake_transport import FakeTransport
from planned_helpers import (
    SIG,
    SIG_OTHER,
    gated_sender,
    logical_body,
    planned_publication,
)

from poster_bot.domain.content import Content, Entity, ForwardSource, MediaRef
from poster_bot.domain.normalization import normalize_content
from poster_bot.domain.planner import plan_post
from poster_bot.domain.signature import SignatureConfig
from poster_bot.domain.states import Status
from poster_bot.repo.plans import PlanStore
from poster_bot.repo.receipts import PartReceiptStore
from poster_bot.repo.sender import SenderService
from poster_bot.runtime import (
    MigrationReadiness,
    plan_migration_readiness,
)


def run(coro):
    return asyncio.run(coro)


def gated_sender_sig(services, transport,
                     signature: SignatureConfig = SIG) -> SenderService:
    """The live gated wiring WITH the configured signature: planless due
    copies are auto-prepared before the claim."""
    return SenderService(
        services.dispatch,
        transport,
        plans=PlanStore(services.conn),
        receipts=PartReceiptStore(services.conn),
        editor=transport,
        signature=signature,
    )


def plan_row(services, post_id: int):
    return services.conn.execute(
        "SELECT * FROM post_plans WHERE post_id=?", (post_id,)).fetchone()


def posts_content_json(services, post_id: int) -> str:
    return services.conn.execute(
        "SELECT content_json FROM posts WHERE id=?", (post_id,)).fetchone(
            )["content_json"]


def schedule(services, content, *, epoch: int = NOW + 7200):
    post_id = services.drafts.create(content, now_epoch=NOW)
    pub = services.publications.schedule(post_id, Instant(epoch),
                                         now_epoch=NOW)
    return pub, post_id


class TestAutoPrepareCopyAtDue:
    def test_signed_plan_frozen_before_claim_published_once(self, services):
        content = text_content("legacy scheduled copy")
        pub, post_id = schedule(services, content)
        transport = FakeTransport()
        sender = gated_sender_sig(services, transport)
        results = run(sender.send_due(now_epoch=pub.scheduled_epoch))
        assert [(r.publication_id, r.outcome) for r in results] == [
            (pub.id, "published")]
        # the persisted pair is EXACTLY what the deterministic planner
        # produces from the stored content under the configured signature
        expected = plan_post(normalize_content(content), SIG).plan
        stored = PlanStore(services.conn).get(post_id)
        assert stored is not None
        assert stored.plan.to_json() == expected.to_json()
        # and the wire bytes are exactly the persisted plan (no replan)
        assert [c.to_json() for c in transport.calls] == \
            [p.content.to_json() for p in stored.plan.parts]
        assert stored.plan.parts[0].content.text.endswith(
            f"{SIG.main_text} | {SIG.partner_text}")
        # the posts row is byte-identical: content, ids and schedule untouched
        assert services.drafts.get_content(post_id) == content
        row = services.conn.execute(
            "SELECT scheduled_epoch, version FROM publications WHERE id=?",
            (pub.id,)).fetchone()
        # the due minute is untouched; the version only advanced through the
        # normal status transitions of the send itself
        assert row["scheduled_epoch"] == pub.scheduled_epoch
        assert row["version"] > pub.version
        assert services.publications.get(pub.id).status is Status.PUBLISHED
        assert transport.notices == []  # no refusal for a prepared copy

    def test_second_tick_never_replans_or_resends(self, services):
        pub, _ = schedule(services, text_content("one shot copy"))
        transport = FakeTransport()
        sender = gated_sender_sig(services, transport)
        run(sender.send_due(now_epoch=pub.scheduled_epoch))
        calls = len(transport.calls)
        run(sender.send_due(now_epoch=pub.scheduled_epoch + 1))
        assert len(transport.calls) == calls  # published stays terminal

    def test_restart_prepared_pair_claims_with_intents(self, services):
        pub, post_id = schedule(services, logical_content_multi())
        transport_a = FakeTransport()
        sender_a = gated_sender_sig(services, transport_a, SIG_LONG)
        # simulate the crash point: preparation persisted, nothing sent
        assert sender_a.prepare_plan(pub, now_epoch=pub.scheduled_epoch)
        assert transport_a.calls == []  # preparation touches no channel
        assert services.conn.execute(
            "SELECT COUNT(*) c FROM send_attempts").fetchone()["c"] == 0
        stored = PlanStore(services.conn).get(post_id)
        assert stored is not None
        assert len(stored.plan.parts) == 2  # the footer forced a split
        updated_after_prep = plan_row(services, post_id)["updated_at"]
        # simulated restart: fresh sender over the same durable state
        transport_b = FakeTransport()
        sender_b = gated_sender_sig(services, transport_b, SIG_LONG)
        results = run(sender_b.send_due(now_epoch=pub.scheduled_epoch))
        assert results[0].outcome == "published"
        attempt = services.conn.execute(
            "SELECT id FROM send_attempts ORDER BY id DESC LIMIT 1").fetchone()
        parts = PartReceiptStore(services.conn).parts_of_attempt(attempt["id"])
        # the claim snapshotted the persisted plan's intents, in order
        assert [p.snapshot.content.to_json() for p in parts] == \
            [p0.content.to_json() for p0 in stored.plan.parts]
        assert all(p.state == "confirmed" for p in parts)
        # the claim never rewrote the plan pair
        assert plan_row(services, post_id)["updated_at"] == updated_after_prep

    def test_embedded_signature_survives_ambient_change(self, services):
        pub, post_id = schedule(services, text_content("ambient copy"))
        before = posts_content_json(services, post_id)
        sender_a = gated_sender_sig(services, FakeTransport(), SIG)
        assert sender_a.prepare_plan(pub, now_epoch=pub.scheduled_epoch)
        assert posts_content_json(services, post_id) == before
        # the ambient config moved to ANOTHER signature before the send
        transport = FakeTransport()
        sender_b = gated_sender_sig(services, transport, SIG_OTHER)
        results = run(sender_b.send_due(now_epoch=pub.scheduled_epoch))
        assert results[0].outcome == "published"
        wire = " ".join(c.to_json() for c in transport.calls)
        assert SIG.main_text in wire and SIG.partner_text in wire
        assert SIG_OTHER.main_text not in wire  # no ambient replan

    def test_album_item_captions_entities_folded_losslessly(self, services):
        first = "🌟 Первая 🌟"
        second = "  leading ws caption"
        content = Content(kind="album", items=(
            MediaRef("photo", "f-one", caption=first,
                     caption_entities=(Entity(type="bold", offset=3,
                                              length=6),)),
            MediaRef("video", "f-two", caption=second)))
        pub, post_id = schedule(services, content)
        transport = FakeTransport()
        run(gated_sender_sig(services, transport)
            .send_due(now_epoch=pub.scheduled_epoch))
        stored = PlanStore(services.conn).get(post_id)
        # captions become trailing body paragraphs in stored item order,
        # verbatim (NO strip), and the bold entity rides the merged offsets
        assert stored.logical.body == f"{first}\n\n{second}"
        bold = [e for e in stored.logical.entities if e.type == "bold"]
        assert len(bold) == 1
        assert bold[0].offset == 3  # "🌟 " is 3 UTF-16 units at part start
        sent = transport.calls[0]
        assert sent.kind == "album" and len(sent.items) == 2
        assert all(item.caption is None for item in sent.items)  # cleared
        assert sent.caption.startswith(first) and second in sent.caption
        assert sent.caption.count("Первая") == 1  # never duplicated
        assert sent.caption.endswith(f"{SIG.main_text} | {SIG.partner_text}")
        assert services.drafts.get_content(post_id) == content

    def test_audio_caption_becomes_signed_body(self, services):
        content = Content(kind="audio",
                          media=MediaRef("audio", "af-1",
                                         caption="Озвучка 🔊"))
        pub, _ = schedule(services, content)
        transport = FakeTransport()
        run(gated_sender_sig(services, transport)
            .send_due(now_epoch=pub.scheduled_epoch))
        sent = transport.calls[0]
        assert sent.kind == "audio" and sent.media.file_id == "af-1"
        assert sent.media.caption.startswith("Озвучка 🔊")
        assert sent.media.caption.endswith(
            f"{SIG.main_text} | {SIG.partner_text}")

    def test_random_unicode_leading_whitespace_no_loss(self, services):
        rng = random.Random(20261023)
        pool = ("abcXYZабвгд 🐍🎨 \n\t" + "😀" + "word ")
        body = "  \n" + "".join(rng.choice(pool) for _ in range(600))
        body = body.replace("\n\n", "\n")  # keep it one paragraph
        body = body[:4096]
        content = Content(kind="text", text=body,
                          entities=(Entity(type="bold", offset=3,
                                           length=10),))
        pub, post_id = schedule(services, content)
        transport = FakeTransport()
        run(gated_sender_sig(services, transport)
            .send_due(now_epoch=pub.scheduled_epoch))
        stored = PlanStore(services.conn).get(post_id)
        assert stored.logical.body == body  # character-for-character
        assert stored.plan.reconstructed_body() == body
        assert transport.calls[0].text.startswith(body[:50])
        assert services.drafts.get_content(post_id) == content


#: a longer operator signature: with its footer, a full-length legacy post
#: (stored COPY text is capped at 4096 chars) no longer fits one part
SIG_LONG = SignatureConfig(
    main_text="НашКанал" * 6, main_url="https://t.me/main_channel",
    partner_text="Партнёр" * 4, partner_url="https://t.example/partner",
)


def logical_content_multi() -> Content:
    """Legacy copy that plans into TWO parts under SIG_LONG (stored text
    stays within the 4096-char content limit the old flow enforced)."""
    return text_content(logical_body((2030, 2028)).body)


def oversized_paragraph() -> Content:
    """One 4096-char paragraph: even the smallest mandatory footer cannot fit
    it into a text part, so the planner blocks with manual_split_required."""
    return text_content("x" * 4096)


def forward_content() -> Content:
    return Content(kind="forward", mode="forward",
                   forward=ForwardSource(chat_id=-100555, message_ids=(42,)))


class TestPreparationRefusal:
    def test_forward_never_auto_converted_warned_once(self, services):
        content = forward_content()
        pub, post_id = schedule(services, content)
        transport = FakeTransport()
        sender = gated_sender_sig(services, transport)
        run(sender.send_due(now_epoch=pub.scheduled_epoch))
        # fail-closed: nothing on the channel, nothing journaled as claimed
        assert transport.calls == [] and transport.edit_calls == []
        assert services.conn.execute(
            "SELECT COUNT(*) c FROM send_attempts").fetchone()["c"] == 0
        assert plan_row(services, post_id) is None
        assert pub_status(services, pub.id) == "scheduled"
        # the original forward stays intact — no implicit copy or strip
        assert services.drafts.get_content(post_id) == content
        # owner warned once per process with id + machine code, no content
        assert len(transport.notices) == 1
        assert str(pub.id) in transport.notices[0]
        assert "forward" in transport.notices[0]
        assert "Код:" in transport.notices[0]
        assert "Откройте предпросмотр" in transport.notices[0]
        run(sender.send_due(now_epoch=pub.scheduled_epoch))  # later tick
        assert len(transport.notices) == 1  # no every-tick spam
        assert transport.calls == []
        assert pub_status(services, pub.id) == "scheduled"

    def test_oversized_paragraph_atomic_rollback_warn_once(self, services):
        content = oversized_paragraph()
        pub, post_id = schedule(services, content)
        transport = FakeTransport()
        sender = gated_sender_sig(services, transport)
        run(sender.send_due(now_epoch=pub.scheduled_epoch))
        # the planner blocks ('manual_split_required'): NO half-plan, NO
        # attempt row, NO clipping, NO channel call — a full rollback
        assert transport.calls == []
        assert plan_row(services, post_id) is None
        assert services.conn.execute(
            "SELECT COUNT(*) c FROM send_attempts").fetchone()["c"] == 0
        assert pub_status(services, pub.id) == "scheduled"
        assert services.drafts.get_content(post_id) == content  # verbatim
        assert len(transport.notices) == 1
        assert str(pub.id) in transport.notices[0]
        assert "manual_split_required" in transport.notices[0]
        run(sender.send_due(now_epoch=pub.scheduled_epoch + 1))
        assert len(transport.notices) == 1
        assert transport.calls == []

    def test_prepare_plan_direct_success_writes_only_the_pair(self, services):
        content = text_content("prepare directly")
        pub, post_id = schedule(services, content)
        before = posts_content_json(services, post_id)
        sender = gated_sender_sig(services, FakeTransport())
        assert sender.prepare_plan(pub, now_epoch=pub.scheduled_epoch)
        assert posts_content_json(services, post_id) == before
        assert services.conn.execute(
            "SELECT COUNT(*) c FROM send_attempts").fetchone()["c"] == 0
        assert plan_row(services, post_id) is not None


class TestNoSignatureKeepsLegacyRefusal:
    """Existing gated fixtures without a signature keep the plain fail-closed
    refusal: auto-prepare is a CONFIGURED-signature behavior only."""

    def test_preparable_copy_refused_unchanged_without_signature(
            self, services):
        pub, post_id = schedule(services, text_content("legacy planless body"))
        transport = FakeTransport()
        sender = gated_sender(services, transport)  # no signature wired
        run(sender.send_due(now_epoch=pub.scheduled_epoch))
        assert transport.calls == []
        assert plan_row(services, post_id) is None
        assert pub_status(services, pub.id) == "scheduled"
        assert any("Откройте предпросмотр" in n and str(pub.id) in n
                   for n in transport.notices)
        assert not any("Код:" in n for n in transport.notices)

    def test_signature_without_plans_is_refused_at_wiring(self, services):
        with pytest.raises(ValueError):
            SenderService(services.dispatch, FakeTransport(), signature=SIG)


def pub_status(services, pub_id: int) -> str:
    return services.conn.execute(
        "SELECT status FROM publications WHERE id=?", (pub_id,)).fetchone(
            )["status"]


class TestMigrationReadiness:
    def _seed(self, services):
        # one minute per publication: the schedule enforces minute uniqueness
        planned_pub, _, _ = planned_publication(services,
                                                logical_body((100, 100)))
        prep_pub, _ = schedule(services, text_content("plain copy"),
                               epoch=NOW + 7260)
        susp_pub, _ = schedule(services, text_content("suspended copy"),
                               epoch=NOW + 7320)
        services.publications.pause(susp_pub.id, susp_pub.version,
                                    now_epoch=NOW)
        fwd_pub, _ = schedule(services, forward_content(), epoch=NOW + 7380)
        over_pub, _ = schedule(services, oversized_paragraph(),
                               epoch=NOW + 7440)
        return planned_pub, prep_pub, susp_pub, fwd_pub, over_pub

    def _codes(self, report: MigrationReadiness) -> dict[int, tuple]:
        return {i.publication_id: i.codes for i in report.items}

    def test_classifies_ids_and_codes_never_content(self, services):
        planned_pub, prep_pub, susp_pub, fwd_pub, over_pub = self._seed(
            services)
        # a legacy-path published post must not be listed at all
        done_pub, _ = schedule(services, text_content("already out"),
                               epoch=NOW + 7500)
        run(SenderService(services.dispatch, FakeTransport())  # ungated
            .send_due(now_epoch=done_pub.scheduled_epoch))
        # a swept-missed one neither
        late_pub, _ = schedule(services, text_content("too late"),
                               epoch=NOW + 60)
        services.dispatch.recovery_sweep(now_epoch=NOW + 7200)
        report = plan_migration_readiness(services.conn, SIG)
        codes = self._codes(report)
        assert codes[planned_pub.id] == ("planned",)
        assert codes[prep_pub.id] == ("preparable",)
        assert codes[susp_pub.id] == ("preparable",)
        assert codes[fwd_pub.id] == ("forward",)
        assert codes[over_pub.id] == ("manual_split_required",)
        assert done_pub.id not in codes and late_pub.id not in codes
        assert report.ready_for_cutover is False
        assert {i.publication_id for i in report.blockers} == \
            {fwd_pub.id, over_pub.id}
        blob = json.dumps([i.codes for i in report.items])
        assert "plain copy" not in blob  # ids + machine codes only

    def test_ready_when_only_planned_and_preparable(self, services):
        planned_publication(services, logical_body((100, 100)))
        schedule(services, text_content("plain copy"), epoch=NOW + 7260)
        report = plan_migration_readiness(services.conn, SIG)
        assert report.ready_for_cutover
        assert sorted(i.codes for i in report.items) == \
            sorted([("planned",), ("preparable",)])

    def test_read_only_no_writes(self, services):
        self._seed(services)
        tables = ("posts", "publications", "post_plans", "publication_parts",
                  "send_attempts", "journal_events", "navigation_edits")
        def snapshot():
            return {t: (services.conn.execute(
                          f"SELECT COUNT(*) FROM {t}").fetchone()[0],
                        services.conn.execute(
                          f"SELECT COALESCE(MAX(rowid),0) FROM {t}").
                        fetchone()[0])
                    for t in tables}
        before = snapshot()
        plan_migration_readiness(services.conn, SIG)
        assert snapshot() == before

    def test_corrupt_plan_pair_is_a_blocker(self, services):
        pub, post_id, plan = planned_publication(services,
                                                 logical_body((100, 100)))
        raw = json.loads(plan.to_json())
        raw["parts"][0] = {
            "index": 1, "body_slice": "",
            "content_json": forward_content().to_json(),
            "footer": None,
        }
        with services.conn:
            services.conn.execute(
                "UPDATE post_plans SET plan_json=? WHERE post_id=?",
                (json.dumps(raw, ensure_ascii=False), post_id))
        report = plan_migration_readiness(services.conn, SIG)
        assert self._codes(report)[pub.id] == ("plan_corrupt",)
        assert not report.ready_for_cutover

    def test_unparseable_stored_content_is_a_blocker(self, services):
        pub, post_id = schedule(services, text_content("plain copy"),
                                epoch=NOW + 7500)
        with services.conn:
            services.conn.execute("UPDATE posts SET content_json=? WHERE id=?",
                                  ('{"kind": "text"}', post_id))
        report = plan_migration_readiness(services.conn, SIG)
        assert self._codes(report)[pub.id] == ("content_invalid",)
        assert not report.ready_for_cutover

    def test_signature_is_required_never_read_from_env(self, services):
        with pytest.raises(ValueError):
            plan_migration_readiness(services.conn, None)
        with pytest.raises(ValueError):
            plan_migration_readiness(services.conn, "@not-a-config")


class TestLiveCompositionAutoPrepare:
    """The real gated LIVE wiring (compose_live) with the configured
    signature: old planless copies publish signed; forwards stay deferred."""

    def _setup(self):
        from aiogram import Bot
        from test_live_composition import (
            LiveApiSession, live_config, GOOD_TOKEN, CHANNEL,
            OWNER as LIVE_OWNER,
        )
        return Bot, LiveApiSession, live_config, GOOD_TOKEN, CHANNEL, LIVE_OWNER

    def test_live_photo_copy_auto_prepares_and_publishes(self, tmp_path):
        Bot, LiveApiSession, live_config, GOOD_TOKEN, CHANNEL, LIVE_OWNER = \
            self._setup()
        from poster_bot.live import compose_live

        async def scenario():
            now = [NOW]
            session = LiveApiSession()
            bot = Bot(GOOD_TOKEN, session=session)
            live = await compose_live(live_config(tmp_path),
                                      clock=lambda: now[0], bot=bot,
                                      now_epoch=NOW)
            try:
                content = Content(kind="photo",
                                  media=MediaRef("photo", "pf-1",
                                                 caption="Фото 📷"))
                post = live.app.drafts.create(content, now_epoch=NOW)
                pub = live.app.publications.schedule(
                    post, Instant(NOW + 30), now_epoch=NOW)
                session.update_script = [[]]
                assert (await live.loop.run_cycle()).ok
                assert live.app.publications.get(
                    pub.id).status is Status.SCHEDULED
                now[0] = NOW + 70  # inside the healthy due window
                session.update_script = [[]]
                assert (await live.loop.run_cycle()).ok
                assert live.app.publications.get(
                    pub.id).status is Status.PUBLISHED
                channel = [(m, p) for m, p in session.requests
                           if p.get("chat_id") == CHANNEL]
                assert [m for m, _ in channel] == ["sendPhoto"]
                sent_caption = channel[0][1]["caption"]
                assert sent_caption.startswith("Фото 📷")
                assert sent_caption.endswith(
                    f"{SIG.main_text} | {SIG.partner_text}")
                # the stored post stays byte-identical; the pair is frozen
                assert live.app.drafts.get_content(post) == content
                assert PlanStore(live.app.conn).get(post) is not None
                # no refusal prompt was ever needed
                assert not [p for m, p in session.requests
                            if p.get("chat_id") == LIVE_OWNER
                            and "не отправлена" in p.get("text", "")]
            finally:
                await live.close()

        run(scenario())

    def test_live_forward_deferred_not_converted_prompt_once(self, tmp_path):
        Bot, LiveApiSession, live_config, GOOD_TOKEN, CHANNEL, LIVE_OWNER = \
            self._setup()
        from poster_bot.live import compose_live

        async def scenario():
            now = [NOW]
            session = LiveApiSession()
            bot = Bot(GOOD_TOKEN, session=session)
            live = await compose_live(live_config(tmp_path),
                                      clock=lambda: now[0], bot=bot,
                                      now_epoch=NOW)
            try:
                content = forward_content()
                post = live.app.drafts.create(content, now_epoch=NOW)
                pub = live.app.publications.schedule(
                    post, Instant(NOW + 30), now_epoch=NOW)
                now[0] = NOW + 70
                session.update_script = [[]]
                assert (await live.loop.run_cycle()).ok
                # fail-closed: nothing into the channel, original intact
                assert not [p for m, p in session.requests
                            if p.get("chat_id") == CHANNEL]
                assert live.app.drafts.get_content(post) == content
                assert PlanStore(live.app.conn).get(post) is None
                assert live.app.publications.get(
                    pub.id).status is Status.SCHEDULED
                prompts = [p for m, p in session.requests
                           if p.get("chat_id") == LIVE_OWNER
                           and "не отправлена" in p.get("text", "")]
                assert len(prompts) == 1
                assert str(pub.id) in prompts[0]["text"]
                assert "forward" in prompts[0]["text"]
                now[0] = NOW + 80  # still inside the window
                session.update_script = [[]]
                assert (await live.loop.run_cycle()).ok
                prompts2 = [p for m, p in session.requests
                            if p.get("chat_id") == LIVE_OWNER
                            and "не отправлена" in p.get("text", "")]
                assert prompts2 == prompts  # no every-tick spam
                assert not [p for m, p in session.requests
                            if p.get("chat_id") == CHANNEL]
            finally:
                await live.close()

        run(scenario())

    def test_startup_preflight_logs_blockers_before_first_minute(
            self, tmp_path, caplog):
        from aiogram import Bot
        from test_live_composition import (
            LiveApiSession, live_config, GOOD_TOKEN, CHANNEL,
        )
        from poster_bot.db import connect, migrate
        from poster_bot.journal import Journal
        from poster_bot.live import compose_live
        from poster_bot.repo.drafts import DraftStore
        from poster_bot.repo.publications import PublicationsService

        config = live_config(tmp_path)
        # seed legacy data BEFORE the app starts (offline, closed conn)
        conn = connect(config.db_path)
        migrate(conn)
        journal = Journal(conn)
        drafts, publications = DraftStore(conn), PublicationsService(conn,
                                                                     journal)
        fwd = drafts.create(forward_content(), now_epoch=NOW)
        publications.schedule(fwd, Instant(NOW + 3600), now_epoch=NOW)
        plain = drafts.create(text_content("plain copy"), now_epoch=NOW)
        publications.schedule(plain, Instant(NOW + 3660), now_epoch=NOW)
        conn.close()

        async def scenario():
            session = LiveApiSession()
            bot = Bot(GOOD_TOKEN, session=session)
            with caplog.at_level(logging.WARNING, logger="poster_bot.live"):
                live = await compose_live(config, clock=lambda: NOW, bot=bot,
                                          now_epoch=NOW)
            try:
                return live, session
            finally:
                await live.close()

        live, session = run(scenario())
        assert live is not None  # the poller composition NEVER blocks on it
        text = caplog.text
        assert "migration readiness blocker" in text
        assert "codes=forward" in text
        assert "plain copy" not in text  # ids+codes only, never content
        # a preparable copy is NOT a blocker and is never logged as one
        assert "codes=preparable" not in text
        assert not [p for m, p in session.requests
                    if p.get("chat_id") == CHANNEL]