"""Nearest twelve and background refresh: isolated DB and fake Telegram only."""
from conftest import NOW, text_content
import pytest

from poster_bot.domain.time import Instant
from poster_bot.domain.states import Status
from poster_bot.repo.notifications import OwnerNoticeStore
from poster_bot.repo.runtime_state import RuntimeState
from poster_bot.runtime import Runtime
from test_ui_handlers_offline import OWNER, _Harness
from fake_transport import FakeTransport
from poster_bot.ui import texts
from poster_bot.ui.state import State
from ui_flow_helpers import build_flow, run, press_label


def planned(services, count=13):
    pubs = []
    for i in range(count):
        post = services.drafts.create(text_content(f"summary-{i + 1:02d}"),
                                      now_epoch=NOW)
        pubs.append(services.publications.schedule(
            post, Instant(NOW + 60 * (i + 1)), now_epoch=NOW))
    return pubs


def test_main_shows_twelve_nearest_not_twelve_paginated_to_six(services):
    flow, _ = build_flow(services)
    pubs = planned(services)
    services.publications.pause(pubs[2].id, pubs[2].version, now_epoch=NOW)
    reply = run(flow.command_start(now=NOW))
    assert reply.text == "📅 Контент-план"
    assert len(reply.rows) == 13
    assert all(len(row) == 1 for row in reply.rows)
    entries = [row[0] for row in reply.rows[:-1]]
    assert len(entries) == 12
    assert [entry.label for entry in entries] == [
        f"21.10 11:{i:02d} | {'⏸' if i == 3 else '🕒'} | 📝 summary-{i:02d}"
        for i in range(1, 13)]
    detail = press_label(flow, reply, "summary-01", now=NOW)
    assert f"Публикация №{pubs[0].id}" in detail.text


def test_empty_dashboard_is_explicit(services):
    flow, _ = build_flow(services)
    assert "Нет запланированных публикаций" in run(flow.command_start(now=NOW)).text


def runtime_for(h, *, hook=None):
    transport = FakeTransport()

    async def refresh(now):
        await h.dashboard.refresh(h.bot, now=now)

    return Runtime(dispatch=h.services.dispatch,
                   notices=OwnerNoticeStore(h.services.conn, h.services.journal),
                   runtime_state=RuntimeState(h.services.conn),
                   transport=transport, refresh_dashboard=hook or refresh), transport


def edits(h):
    return [p for m, p in h.session.requests if m == "editMessageText"]


def action_count(services):
    return services.conn.execute("SELECT COUNT(*) FROM owner_actions").fetchone()[0]


def test_tick_commits_publish_then_edits_same_main_and_fills_thirteenth(services, monkeypatch):
    h = _Harness(services)
    pubs = planned(services)
    h.message(text="/start")
    card, sends, state = h.card_id(), len(h.sent()), h.ui.get()
    original = h.bot.edit_message_text
    observations = []

    async def observe(**kwargs):
        observations.append((services.publications.get(pubs[0].id).status,
                             services.dispatch.history_links(pubs[0].post_id),
                             services.conn.in_transaction))
        return await original(**kwargs)

    monkeypatch.setattr(h.bot, "edit_message_text", observe)
    rt, transport = runtime_for(h)
    report = run(rt.tick(now_epoch=NOW + 60))
    assert report.sent[0].outcome == "published"
    assert observations[0][0] is Status.PUBLISHED
    assert observations[0][1] and observations[0][2] is False
    assert len(transport.calls) == 1
    assert len(h.sent()) == sends
    assert len(edits(h)) == 1 and edits(h)[0]["message_id"] == card
    assert h.card_id() == card and h.ui.get() == state
    assert h.last_text() == "📅 Контент-план"
    assert not any("summary-01" in label for label in h.last_labels())
    assert any("summary-13" in label for label in h.last_labels())
    assert sum(" | 🕒 | 📝 summary-" in label for label in h.last_labels()) == 12
    assert all(len(row) == 1 for row in edits(h)[0]["reply_markup"]["inline_keyboard"])
    assert len(edits(h)[0]["reply_markup"]["inline_keyboard"]) == 13
    before = action_count(services)
    run(rt.tick(now_epoch=NOW + 61))
    run(rt.tick(now_epoch=NOW + 62))
    assert len(edits(h)) == 1 and len(transport.calls) == 1
    assert action_count(services) == before


def test_refresh_error_retries_only_ui_without_republish_or_new_tokens(
    services, monkeypatch, caplog
):
    h = _Harness(services)
    pubs = planned(services)
    h.message(text="/start")
    original = h.bot.edit_message_text
    attempts = []

    async def flaky(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise RuntimeError("secret post text must not appear in logs")
        return await original(**kwargs)

    monkeypatch.setattr(h.bot, "edit_message_text", flaky)
    rt, transport = runtime_for(h)
    run(rt.tick(now_epoch=NOW + 60))
    count = action_count(services)
    assert services.publications.get(pubs[0].id).status is Status.PUBLISHED
    run(rt.tick(now_epoch=NOW + 61))  # bounded retry: no API spam
    assert len(attempts) == 1
    run(rt.tick(now_epoch=NOW + 62))
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert action_count(services) == count
    assert len(transport.calls) == 1
    assert h.last_text() == "📅 Контент-план"
    assert any("summary-13" in label for label in h.last_labels())
    assert not any("summary-01" in label for label in h.last_labels())
    assert "secret post" not in caplog.text


@pytest.mark.parametrize("screen", ["creating", "editing", "detail", "day", "settings"])
def test_background_never_hijacks_other_screen(services, screen):
    h = _Harness(services)
    pubs = planned(services, 1)
    h.message(text="/start")
    if screen == "creating":
        h.press_text(texts.MENU_NEW_POST)
        h.message(text="unfinished content", message_id=41)
    elif screen == "editing":
        h.flow._go(State.EDITING, {"pub_id": pubs[0].id}, now=NOW)
    elif screen == "detail":
        h.press_text(next(label for label in h.last_labels() if "summary-" in label))
    elif screen == "day":
        h.press_text(texts.MENU_PLAN)
    else:
        h.press_text(texts.MENU_SETTINGS)
    before = (list(h.session.requests), h.ui.get(), h.card_id(), dict(h.session.visible))
    rt, transport = runtime_for(h)
    run(rt.tick(now_epoch=NOW + 60))
    assert len(transport.calls) == 1
    assert (h.session.requests, h.ui.get(), h.card_id(), h.session.visible) == before


@pytest.mark.parametrize("missing_on_server", [False, True])
def test_missing_dashboard_is_never_resurrected_until_start(services, missing_on_server):
    h = _Harness(services)
    planned(services, 1)
    h.message(text="/start")
    old, sends = h.card_id(), len(h.sent())
    h.session.visible.clear()  # owner cleared local history: server cannot detect
    if missing_on_server:
        h.session.edit_error = "Bad Request: message to edit not found"
    rt, _ = runtime_for(h)
    run(rt.tick(now_epoch=NOW + 60))
    run(rt.tick(now_epoch=NOW + 62))
    assert len(h.sent()) == sends and not h.session.visible
    assert len(edits(h)) == 1
    h.message(text="/start", message_id=99)
    assert h.card_id() != old
    assert "Нет запланированных публикаций" in h.last_text()
    assert h.card_id() in h.session.visible


def test_paused_never_sends_and_timezone_change_redisplays_without_moving_epoch(services):
    h = _Harness(services)
    pub = planned(services, 1)[0]
    services.publications.pause(pub.id, pub.version, now_epoch=NOW)
    h.message(text="/start")
    assert "21.10 11:01 | ⏸ | 📝 summary-01" in h.last_labels()
    services.settings.change(timezone="Pacific/Honolulu")
    rt, transport = runtime_for(h)
    run(rt.tick(now_epoch=NOW + 60))
    assert "20.10 22:01 | ⏸ | 📝 summary-01" in h.last_labels()
    assert h.last_text() == "📅 Контент-план"
    assert not transport.calls
    assert services.publications.get(pub.id).scheduled_epoch == pub.scheduled_epoch
    h.message(text="/start")
    assert "20.10 22:01 | ⏸ | 📝 summary-01" in h.last_labels()


def test_unexpected_injected_hook_exception_cannot_escape_sender_tick(services):
    h = _Harness(services)
    pub = planned(services, 1)[0]

    async def broken(now):
        assert services.publications.get(pub.id).status is Status.PUBLISHED
        raise RuntimeError("isolated UI failure")

    rt, transport = runtime_for(h, hook=broken)
    assert run(rt.tick(now_epoch=NOW + 60)).sent[0].outcome == "published"
    run(rt.tick(now_epoch=NOW + 61))
    assert len(transport.calls) == 1


def test_query_orders_epochs_not_creation_and_excludes_processed_statuses(services):
    flow, _ = build_flow(services)
    pubs = []
    for minutes in (20, 1, 30, 10, 40, 50):
        post = services.drafts.create(text_content(f"at-{minutes}"), now_epoch=NOW)
        pubs.append(services.publications.schedule(post, Instant(NOW + minutes * 60),
                                                   now_epoch=NOW))
    for pub, outcome in zip(pubs[2:], ("published", "missed", "uncertain", "sending")):
        attempt = services.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch)
        services.dispatch.mark_in_progress(attempt, now_epoch=pub.scheduled_epoch)
        if outcome == "published":
            services.dispatch.complete(pub.id, attempt, (999,), "https://t.me/c/555/999",
                                       now_epoch=pub.scheduled_epoch)
        elif outcome == "missed":
            services.dispatch.fail_definitive(pub.id, attempt, "api_error",
                                              now_epoch=pub.scheduled_epoch)
        elif outcome == "uncertain":
            services.dispatch.mark_uncertain(pub.id, attempt, "timeout",
                                             now_epoch=pub.scheduled_epoch)
    data = flow.dashboard_snapshot()
    assert [e.publication_id for e in data.entries] == [pubs[1].id, pubs[0].id]


def test_old_dashboard_selection_rechecks_publication_version(services):
    flow, _ = build_flow(services)
    pub = planned(services, 1)[0]
    reply = run(flow.command_start(now=NOW))
    services.publications.pause(pub.id, pub.version, now_epoch=NOW)
    state = flow._ui()
    result = press_label(flow, reply, "summary-01", now=NOW)
    assert result.stale_callback and flow._ui() == state


def test_summary_is_display_only_bounded_plain_and_keeps_all_navigation(services):
    flow, _ = build_flow(services)
    original = "🔥 <b>hello</b>\n\u202e" + "long" * 400
    post = services.drafts.create(text_content(original), now_epoch=NOW)
    services.publications.schedule(post, Instant(NOW + 60), now_epoch=NOW)
    reply = run(flow.command_start(now=NOW))
    assert "\u202e" not in reply.rows[0][0].label and "…" in reply.rows[0][0].label
    assert "📝 🔥 <b>hello</b>" in reply.rows[0][0].label
    assert reply.text == "📅 Контент-план"
    assert len(reply.text) < 4096
    assert services.drafts.get_content(post).text == original
    all_labels = [b.label for row in reply.rows for b in row]
    assert texts.MENU_JOURNAL in all_labels
    assert not any(label in all_labels for label in (texts.MENU_NEW_POST,
                   texts.MENU_DRAFTS, texts.MENU_PLAN, texts.MENU_SETTINGS))


@pytest.mark.parametrize("count", [0, 3, 12, 13])
def test_dispatcher_dashboard_one_column_without_duplicate_persistent_menu(services, count):
    h = _Harness(services)
    planned(services, count)
    h.message(text="/start")
    assert len(h.sent()) == 2  # keyboard installation and the actual service card
    keys = {button["text"].casefold() for row in h.sent()[0]["reply_markup"]["keyboard"]
            for button in row}
    assert keys == {label.casefold() for label in (texts.MENU_NEW_POST, texts.MENU_DRAFTS,
                    texts.MENU_PLAN, texts.MENU_SETTINGS, "Рестарт")}
    rows = h.sent()[-1]["reply_markup"]["inline_keyboard"]
    assert len(rows) == min(count, 12) + 1
    assert all(len(row) == 1 for row in rows)
    assert not keys.intersection(button["text"].casefold() for row in rows for button in row)
    assert [row[0]["text"] for row in rows[:-1]] == [
        f"21.10 11:{i:02d} | 🕒 | 📝 summary-{i:02d}" for i in range(1, min(count, 12) + 1)]
    h.press_text(texts.MENU_JOURNAL)
    assert "Журнал" in h.last_text()
    h.press_text("Назад")
    assert "Контент-план" in h.last_text()


def test_dashboard_buttons_keep_dates_across_days_and_timezone_changes(services):
    flow, _ = build_flow(services)
    for i, offset in enumerate((86400 + 120, 60)):
        post = services.drafts.create(text_content(f"day-{i}"), now_epoch=NOW)
        services.publications.schedule(post, Instant(NOW + offset), now_epoch=NOW)
    first = run(flow.command_start(now=NOW))
    assert [row[0].label for row in first.rows[:-1]] == [
        "21.10 11:01 | 🕒 | 📝 day-1", "22.10 11:02 | 🕒 | 📝 day-0"]
    services.settings.change(timezone="Pacific/Honolulu")
    changed = flow.dashboard_reply(now=NOW, snapshot=flow.dashboard_snapshot())
    assert [row[0].label for row in changed.rows[:-1]] == [
        "20.10 22:01 | 🕒 | 📝 day-1", "21.10 22:02 | 🕒 | 📝 day-0"]


def test_neutral_menu_fallback_does_not_restore_duplicate_buttons(services):
    flow, _ = build_flow(services)
    flow._go(State.CREATING, {}, now=NOW)
    reply = flow._menu_reply(NOW)
    assert reply.dashboard is None
    assert [[button.label for button in row] for row in reply.rows] == [[texts.MENU_JOURNAL]]


@pytest.mark.parametrize("kind", ["photo", "video", "audio", "album", "forward"])
@pytest.mark.parametrize("with_caption", [False, True])
def test_dashboard_buttons_reuse_stored_media_caption_or_kind_fallback(services, kind, with_caption):
    from poster_bot.domain.content import Content, ForwardSource, MediaRef
    flow, _ = build_flow(services)
    caption = "Actual media caption" if with_caption else None
    if kind == "forward":
        content = Content(kind=kind, mode="forward", forward=ForwardSource(111, (44,)))
    elif kind == "album":
        content = Content(kind=kind, items=(MediaRef("photo", "a", caption), MediaRef("video", "b")))
    else:
        content = Content(kind=kind, media=MediaRef(kind, "a", caption))
    post = services.drafts.create(content, now_epoch=NOW)
    services.publications.schedule(post, Instant(NOW + 60), now_epoch=NOW)
    reply = run(flow.command_start(now=NOW))
    excerpt = caption if caption and kind != "forward" else texts.KIND_RU[kind]
    icon = {"photo": "📷", "video": "🎬", "audio": "🎵", "album": "🖼", "forward": "↪️"}[kind]
    assert reply.rows[0][0].label == f"21.10 11:01 | 🕒 | {icon} {excerpt}"
    assert reply.text == "📅 Контент-план"
    assert services.drafts.get_content(post) == content


def test_real_live_composition_polls_publishes_then_refreshes_existing_card(tmp_path):
    from aiogram import Bot
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import GetUpdates
    from test_live_composition import (
        LiveApiSession, live_config, start_update, GOOD_TOKEN, OWNER as LIVE_OWNER, CHANNEL,
    )
    from poster_bot.live import compose_live

    async def scenario():
        now = [NOW]
        session = LiveApiSession()
        bot = Bot(GOOD_TOKEN, session=session)
        live = await compose_live(live_config(tmp_path), clock=lambda: now[0],
                                  bot=bot, now_epoch=NOW)
        try:
            pubs = planned(live.app)
            session.update_script = [[start_update(1)]]
            assert (await live.loop.run_cycle()).ok
            from poster_bot.ui.navigation import NavigationStore
            navigation = NavigationStore(live.app.conn, LIVE_OWNER)
            card = navigation.get(LIVE_OWNER)
            session.update_script = [TelegramNetworkError(
                method=GetUpdates(), message="offline simulated outage")]
            now[0] = NOW + 10
            assert not (await live.loop.run_cycle()).ok
            assert not [p for m, p in session.requests if m == "editMessageText"]
            now[0] = NOW + 20  # reconnect before due; recovery must not catch up
            assert (await live.loop.run_cycle()).ok
            now[0] = NOW + 60
            assert (await live.loop.run_cycle()).ok
            assert live.app.publications.get(pubs[0].id).status is Status.PUBLISHED
            assert navigation.get(LIVE_OWNER) == card
            changes = [p for m, p in session.requests if m == "editMessageText"]
            assert len(changes) == 1 and changes[0]["message_id"] == card
            assert changes[0]["chat_id"] == LIVE_OWNER
            assert changes[0]["text"] == "📅 Контент-план"
            updated_labels = [row[0]["text"] for row in
                              changes[0]["reply_markup"]["inline_keyboard"]]
            assert any("summary-13" in label for label in updated_labels)
            assert not any("summary-01" in label for label in updated_labels)
            channel_calls = [p for m, p in session.requests
                             if m == "sendMessage" and p["chat_id"] == CHANNEL]
            assert len(channel_calls) == 1
            now[0] += 1
            await live.loop.run_cycle()
            assert len([p for m, p in session.requests if m == "editMessageText"]) == 1
        finally:
            await live.close()

    run(scenario())
