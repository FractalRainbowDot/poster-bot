"""Bottom-menu and Restart regressions through real offline Dispatcher routing."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import NOW, text_content
from poster_bot.domain.states import Status
from poster_bot.domain.time import Instant
from poster_bot.ui import texts
from poster_bot.ui.state import State
from test_ui_handlers_offline import OWNER, _Harness
from test_ui_navigation import callback
from ui_flow_helpers import run


EXPECTED_ROWS = [
    [texts.MENU_NEW_POST, texts.MENU_DRAFTS],
    [texts.MENU_PLAN, texts.MENU_SETTINGS],
    [texts.MENU_JOURNAL, texts.MENU_RESTART],
]


def test_start_uses_the_same_all_status_day_plan_as_restart(services):
    h = _Harness(services)
    post = services.drafts.create(text_content("already-published"), now_epoch=NOW)
    pub = services.publications.schedule(post, Instant(NOW + 60), now_epoch=NOW)
    attempt = services.dispatch.claim(pub.id, pub.version, now_epoch=NOW + 60)
    services.dispatch.mark_in_progress(attempt, now_epoch=NOW + 60)
    services.dispatch.complete(pub.id, attempt, (1001,), "https://t.me/c/555/1001",
                               now_epoch=NOW + 60)
    h.message(text="/start")
    assert any("already-published" in label and "✅" in label for label in h.last_labels())
    assert "21.10.2026" in h.last_text()
    assert "← Пред." in h.last_labels() and "След. →" in h.last_labels()
    expected = h.last_text(), labels(h)
    for i, command in enumerate((texts.MENU_RESTART, texts.MENU_PLAN, "/start"), 40):
        h.message(text=command, message_id=i)
        assert (h.last_text(), labels(h)) == expected


def labels(h):
    return [[button["text"] for button in row] for row in
            h.cards()[-1].get("reply_markup", {}).get("inline_keyboard", [])]


def test_all_six_bottom_buttons_have_icons_and_no_inline_duplicate(services):
    h = _Harness(services)
    h.message(text="/start")
    keyboard = h.sent()[0]["reply_markup"]
    assert [[button["text"] for button in row] for row in keyboard["keyboard"]] == EXPECTED_ROWS
    assert keyboard["resize_keyboard"] and keyboard["is_persistent"]
    assert labels(h) == [["← Пред.", "Сегодня", "След. →"], ["Создать пост"]]
    h.message(text=texts.MENU_SETTINGS, message_id=40)
    assert not any("Журнал" in label for row in labels(h) for label in row)
    h.message(text=texts.MENU_JOURNAL, message_id=41)
    assert texts.TITLE_JOURNAL in h.last_text()
    assert 41 not in h.session.visible
    assert len([p for p in h.session.visible.values()
                if "keyboard" in p.get("reply_markup", {})]) == 1


@pytest.mark.parametrize("bare,decorated,state,view", [
    ("Создать пост", texts.MENU_NEW_POST, State.CREATING, None),
    ("Отложенное", texts.MENU_DRAFTS, State.MENU, "drafts"),
    ("Черновики", texts.MENU_DRAFTS, State.MENU, "drafts"),
    ("Контент-план", texts.MENU_PLAN, State.MENU, "day"),
    ("Настройки", texts.MENU_SETTINGS, State.SETTINGS, None),
    ("Журнал", texts.MENU_JOURNAL, State.MENU, "journal"),
    ("Рестарт", texts.MENU_RESTART, State.MENU, "day"),
])
def test_old_and_new_labels_route_identically_from_intake(services, bare, decorated, state, view):
    h = _Harness(services)
    for i, label in enumerate((bare, decorated)):
        h.message(text="Создать пост", message_id=10 + i * 3)
        h.message(text="unfinished", message_id=11 + i * 3)
        h.message(text=label, message_id=12 + i * 3)
        assert h.ui.get().state is state
        if view:
            assert h.ui.get().payload["view"] == view
        if i == 0:
            expected = (h.last_text(), labels(h))
        else:
            assert (h.last_text(), labels(h)) == expected
    assert services.drafts.list_drafts() == []
    assert h.preview.calls == [] and h.preview.owner_previews == []


def test_restart_matches_day_plan_with_all_statuses_and_thirteen_rows(services):
    h = _Harness(services)
    for i in range(13):
        post = services.drafts.create(text_content(f"day-post-{i:02d}"), now_epoch=NOW)
        when = NOW + 60 * (i + 1)
        pub = services.publications.schedule(post, Instant(when), now_epoch=NOW)
        status = list(Status)[i % len(Status)]
        if status is Status.SUSPENDED:
            services.publications.pause(pub.id, pub.version, now_epoch=NOW)
        elif status not in (Status.SCHEDULED, Status.SUSPENDED):
            attempt = services.dispatch.claim(pub.id, pub.version, now_epoch=when)
            services.dispatch.mark_in_progress(attempt, now_epoch=when)
            if status is Status.PUBLISHED:
                services.dispatch.complete(pub.id, attempt, (1000 + i,),
                    f"https://t.me/c/555/{1000 + i}", now_epoch=when)
            elif status is Status.UNCERTAIN:
                services.dispatch.mark_uncertain(pub.id, attempt, "timeout", now_epoch=when)
            elif status is Status.MISSED:
                services.dispatch.fail_definitive(pub.id, attempt, "api_error", now_epoch=when)
    h.message(text="Контент-план")
    expected = (h.last_text(), labels(h))
    h.message(text=texts.MENU_RESTART, message_id=40)
    assert (h.last_text(), labels(h)) == expected
    assert "1–10 из 13" in h.last_text()
    assert {texts.status_marker(status) for status in Status} <= {
        row[0].split(" | ")[1] for row in labels(h) if " | " in row[0]}
    seen = [row[0] for row in labels(h) if "day-post-" in row[0]]
    h.press_text("Ещё ↓ 2/2")
    seen += [row[0] for row in labels(h) if "day-post-" in row[0]]
    assert len(seen) == len(set(seen)) == 13
    assert "11–13 из 13" in h.last_text()
    h.press_text("↑ Сверху")
    assert (h.last_text(), labels(h)) == expected
    h.press_text("← Пред.")
    assert "20.10.2026" in h.last_text()
    h.press_text("След. →")
    assert (h.last_text(), labels(h)) == expected
    h.press_text("Сегодня")
    assert (h.last_text(), labels(h)) == expected
    assert h.preview.calls == [] and h.preview.owner_previews == []
    # All entry points now arm the same all-status day presentation.
    h.message(text="/start", message_id=41)
    assert h.dashboard._frame is not None
    h.message(text="Рестарт", message_id=42)
    before = list(h.session.requests)
    run(h.dashboard.refresh(h.bot, now=NOW + 60))
    assert h.session.requests == before and h.dashboard._frame is not None


@pytest.mark.parametrize("zone,day", [("Pacific/Honolulu", "20.10.2026"),
                                     ("Pacific/Kiritimati", "21.10.2026")])
def test_restart_uses_current_local_day_across_midnight(services, zone, day):
    h = _Harness(services)
    services.settings.change(timezone=zone)
    h.now = int(datetime(2026, 10, 21, 0, 5, tzinfo=ZoneInfo("UTC")).timestamp())
    h.message(text="Контент-план")
    expected = (h.last_text(), labels(h))
    h.message(text="Рестарт", message_id=40)
    assert (h.last_text(), labels(h)) == expected
    assert day in h.last_text() and zone in h.last_text()


@pytest.mark.parametrize("step", ["intake", "confirmation"])
@pytest.mark.parametrize("command", ["Рестарт", "/start", "/cancel"])
def test_restart_invalidates_old_steps_without_mutating_posts_or_sources(services, step, command):
    from poster_bot.transport.aiogram_channel import AiogramChannelTransport
    h = _Harness(services)
    h.flow._preview = AiogramChannelTransport(h.bot, channel_id=-100555, owner_id=OWNER)
    h.message(text="Создать пост")
    h.message(text="source", message_id=30,
              forward_origin={"type": "hidden_user", "date": NOW,
                              "sender_user_name": "source"})
    source_token, source_card = h._button_data(texts.BTN_CONTINUE), h.card_id()
    if step == "confirmation":
        h.press_text(texts.BTN_FORWARD_AS_IS)
        h.message(text="12:00", message_id=31)
        source_token, source_card = h._button_data(texts.BTN_CONFIRM), h.card_id()
    before_posts = services.conn.execute("SELECT * FROM posts").fetchall()
    h.message(text=command, message_id=40)
    assert h.ui.get().payload == {"view": "day", "day": "2026-10-21", "page": 0}
    assert 30 in h.session.visible  # forward coordinates remain usable
    assert len(h.session.visible) == 3  # source, one keyboard prompt, day card
    assert list(h.session.visible)[-1] == h.card_id()
    callback(h, source_token, source_card)
    assert services.conn.execute("SELECT * FROM posts").fetchall() == before_posts
    assert services.conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 0
    assert all(p.get("chat_id", OWNER) == OWNER for _, p in h.session.requests)


@pytest.mark.parametrize("destination", [texts.MENU_SETTINGS, texts.MENU_JOURNAL,
                                        texts.MENU_DRAFTS, texts.MENU_NEW_POST,
                                        "publication", "legacy_menu"])
def test_every_top_level_return_uses_the_same_day_plan(services, destination):
    h = _Harness(services)
    post = services.drafts.create(text_content("visible after publish"), now_epoch=NOW)
    pub = services.publications.schedule(post, Instant(NOW + 60), now_epoch=NOW)
    attempt = services.dispatch.claim(pub.id, pub.version, now_epoch=NOW + 60)
    services.dispatch.mark_in_progress(attempt, now_epoch=NOW + 60)
    services.dispatch.complete(pub.id, attempt, (1002,), "https://t.me/c/555/1002",
                               now_epoch=NOW + 60)
    h.message(text="/start")
    expected = h.last_text(), labels(h)
    if destination == "legacy_menu":
        token = h.flow._token(h.flow.A_MENU, now=NOW)
        callback(h, token, h.card_id())
    else:
        if destination == "publication":
            h.press_text(next(label for label in h.last_labels()
                              if "visible after publish" in label))
        else:
            h.press_text(destination)
        h.press_text(texts.BTN_CANCEL if destination == texts.MENU_NEW_POST else texts.BTN_BACK)
    assert (h.last_text(), labels(h)) == expected
    assert h.dashboard._frame is not None
    assert services.publications.get(pub.id).status is Status.PUBLISHED
    assert not h.preview.calls


def test_wrong_stage_text_preserves_selected_day_and_page(services):
    h = _Harness(services)
    for i in range(13):
        post = services.drafts.create(text_content(f"page-{i}"), now_epoch=NOW)
        services.publications.schedule(post, Instant(NOW + 86400 + 60 * (i + 1)),
                                       now_epoch=NOW)
    h.message(text="/start")
    h.press_text("След. →")
    h.press_text("Ещё ↓ 2/2")
    expected = h.last_text(), labels(h), h.ui.get()
    h.message(text="irrelevant text", message_id=40)
    assert (h.last_text(), labels(h), h.ui.get()) == expected
    assert "22.10.2026" in h.last_text() and "11–13 из 13" in h.last_text()
    assert not h.preview.calls and not h.preview.owner_previews


@pytest.mark.parametrize("label", [texts.MENU_JOURNAL, "Журнал", texts.MENU_RESTART, "Рестарт", "/cancel"])
@pytest.mark.parametrize("user,chat", [(999, OWNER), (OWNER, -555), (OWNER, 999)])
def test_journal_and_restart_guard_before_effects(services, label, user, chat):
    h = _Harness(services)
    h.message(text=label, chat={"id": chat, "type": "group" if chat < 0 else "private"},
              **{"from": {"id": user, "is_bot": False, "first_name": "x"}})
    assert h.session.requests == [] and h.ui.get().version == 0


@pytest.mark.parametrize("destination", [texts.MENU_JOURNAL, texts.MENU_RESTART])
def test_menu_cleans_old_preview_only_after_new_card_is_delivered(services, destination):
    from poster_bot.transport.aiogram_channel import AiogramChannelTransport
    h = _Harness(services)
    h.flow._preview = AiogramChannelTransport(h.bot, channel_id=-100555, owner_id=OWNER)
    h.message(text="/start")
    h.message(text="Создать пост", message_id=20)
    h.message(text="private preview", message_id=30)
    h.press_text(texts.BTN_CONTINUE)
    preview_id = next(mid for mid, p in h.session.visible.items()
                      if p.get("text") == "private preview")
    old_card = h.card_id()
    observations = []

    def before_delete(mid):
        if mid == preview_id:
            observations.append((h.card_id(), list(h.session.visible)[-1]))

    h.session.before_delete = before_delete
    h.message(text=destination, message_id=40)
    assert observations == [(h.card_id(), h.card_id())]
    assert preview_id not in h.session.visible and old_card not in h.session.visible
    assert len(h.session.visible) == 2
    assert services.drafts.get_content(services.drafts.list_drafts()[0]).text == "private preview"
    assert all(p.get("chat_id", OWNER) == OWNER for _, p in h.session.requests)


@pytest.mark.parametrize("label", [texts.MENU_JOURNAL, texts.MENU_RESTART, texts.MENU_NEW_POST,
                                   texts.MENU_DRAFTS, texts.MENU_PLAN, texts.MENU_SETTINGS, "/cancel"])
def test_decorated_forward_labels_remain_sources_not_commands(services, label):
    h = _Harness(services)
    h.message(text="Создать пост")
    h.message(text=label, message_id=30,
              forward_origin={"type": "hidden_user", "date": NOW,
                              "sender_user_name": "source"})
    assert h.ui.get().state is State.CREATING
    assert h.ui.get().payload["staged"]["kind"] == "forward"
    assert 30 in h.session.visible


@pytest.mark.parametrize("label", ["📒 Журнал ", "🔄 Рестарт ", "🔥 Контент-план"])
def test_only_exact_menu_aliases_override_intake(services, label):
    h = _Harness(services)
    h.message(text="Создать пост")
    h.message(text=label, message_id=30)
    assert h.ui.get().state is State.CREATING
    assert h.ui.get().payload["staged"]["text"] == label
