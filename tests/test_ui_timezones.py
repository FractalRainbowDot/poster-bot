"""IANA picker: deterministic pagination, current selection, and no rescheduling."""
from datetime import datetime, timezone

from conftest import NOW, future, text_content
from ui_flow_helpers import build_flow, press_label, run, find_token
from test_ui_handlers_offline import OWNER, _Harness
from poster_bot.ui import texts, timezones
from poster_bot.ui.actions import ActionStore


def labels(reply):
    return [button.label for row in reply.rows for button in row]


def picker(flow):
    return press_label(flow, flow.settings_view(now=NOW), texts.SET_TZ_LABEL, now=NOW)


def test_first_page_moscow_current_and_navigation_bounds(services):
    flow, _ = build_flow(services)
    first = picker(flow)
    assert labels(first)[0] == "✓ Москва · UTC+3"
    assert "← Пред." not in labels(first)
    assert "След. →" in labels(first)
    assert "1/4" in first.text
    pages = [first]
    while "След. →" in labels(pages[-1]):
        pages.append(press_label(flow, pages[-1], "След. →", now=NOW))
    assert len(pages) == 4
    assert "← Пред." in labels(pages[-1])
    assert "След. →" not in labels(pages[-1])
    assert services.settings.get().timezone == "Europe/Moscow"
    previous = press_label(flow, pages[-1], "← Пред.", now=NOW)
    assert "3/4" in previous.text


def test_choose_persists_confirms_and_does_not_shift_existing_utc(services):
    flow, _ = build_flow(services)
    post = services.drafts.create(text_content(), now_epoch=NOW)
    pub = services.publications.schedule(post, future(), now_epoch=NOW)
    reply = picker(flow)
    done = press_label(flow, reply, "Екатеринбург", now=NOW)
    assert done.text.startswith(texts.TITLE_SETTINGS)
    assert "Asia/Yekaterinburg" in done.text
    assert services.settings.get().timezone == "Asia/Yekaterinburg"
    actual = services.publications.get(pub.id)
    assert (actual.scheduled_epoch, actual.minute_epoch, actual.version) == (
        pub.scheduled_epoch, pub.minute_epoch, pub.version)
    assert any(row["kind"] == "settings_changed"
               for row in services.journal.entries_page(0, 20))
    flow2, _ = build_flow(services)
    assert labels(picker(flow2))[3].startswith("✓ Екатеринбург")


def test_current_zone_opens_preselected_page(services):
    flow, _ = build_flow(services)
    services.settings.change(timezone="Asia/Vladivostok")
    reply = picker(flow)
    assert "2/4" in reply.text
    assert "✓ Владивосток · UTC+10" in labels(reply)
    assert timezones.choices()[0] == "Europe/Moscow"


def test_noncurated_current_zone_still_selectable(services):
    flow, _ = build_flow(services)
    services.settings.change(timezone="Pacific/Chatham")
    reply = picker(flow)
    assert any(label.startswith("✓ Chatham · UTC+13:45") for label in labels(reply))


def test_search_does_not_save_until_button_choose(services):
    flow, _ = build_flow(services)
    picker(flow)
    found = run(flow.text(now=NOW, text="берлин"))
    assert "Берлин" in labels(found)[0]
    assert services.settings.get().timezone == "Europe/Moscow"
    done = press_label(flow, found, "Берлин", now=NOW)
    assert services.settings.get().timezone == "Europe/Berlin"
    assert "Europe/Berlin" in done.text


def test_search_all_iana_and_empty_results(services):
    flow, _ = build_flow(services)
    picker(flow)
    found = run(flow.text(now=NOW, text="Pacific/Chatham"))
    assert "Chatham" in labels(found)[0]
    empty = run(flow.text(now=NOW, text="not/a/timezone"))
    assert "Ничего не найдено" in empty.text
    assert "След. →" not in labels(empty)
    assert "← Пред." not in labels(empty)
    assert services.settings.get().timezone == "Europe/Moscow"


def test_all_zones_paginate_deterministically_without_saving(services):
    flow, _ = build_flow(services)
    reply = press_label(flow, picker(flow), "Все пояса IANA", now=NOW)
    expected = timezones.choices(all_zones=True)
    assert len(expected) > len(timezones.COMMON)
    keys = []
    while True:
        keys.extend(ActionStore(services.conn).peek(button.token).arg
                    for row in reply.rows for button in row
                    if ActionStore(services.conn).peek(button.token).action == "tz_choose")
        if "След. →" not in labels(reply):
            break
        reply = press_label(flow, reply, "След. →", now=NOW)
    assert keys == expected
    assert services.settings.get().timezone == "Europe/Moscow"


def test_stale_picker_cannot_choose_after_new_page_or_navigation(services):
    flow, _ = build_flow(services)
    first = picker(flow)
    old = find_token(first, "Калининград")
    second = press_label(flow, first, "След. →", now=NOW)
    assert run(flow.press(now=NOW, token=old)).text == texts.BUTTON_STALE
    another = find_token(second, "Красноярск")
    run(flow.new_post(now=NOW))
    assert run(flow.press(now=NOW, token=another)).text == texts.BUTTON_STALE
    assert services.settings.get().timezone == "Europe/Moscow"


def test_optional_manual_input_invalid_and_valid(services):
    flow, _ = build_flow(services)
    reply = press_label(flow, picker(flow), "Ввести IANA вручную", now=NOW)
    assert "Europe/Moscow" in reply.text
    bad = run(flow.text(now=NOW, text="Mars/Olympus"))
    assert bad.text.startswith(texts.SETTINGS_BAD_TZ)
    assert services.settings.get().timezone == "Europe/Moscow"
    done = run(flow.text(now=NOW, text="Pacific/Chatham"))
    assert "Pacific/Chatham" in done.text
    assert services.settings.get().timezone == "Pacific/Chatham"


def test_offset_is_now_sensitive_and_fractional():
    winter = int(datetime(2026, 1, 15, tzinfo=timezone.utc).timestamp())
    summer = int(datetime(2026, 7, 15, tzinfo=timezone.utc).timestamp())
    assert "UTC+1" in timezones.label("Europe/Berlin", current="", now=winter)
    assert "UTC+2" in timezones.label("Europe/Berlin", current="", now=summer)
    assert "UTC−5" in timezones.label("America/New_York", current="", now=winter)
    assert "UTC+5:45" in timezones.label("Asia/Kathmandu", current="", now=summer)


def test_dispatcher_settings_picker_search_choose_keeps_bottom_card(services):
    h = _Harness(services)
    h.message(text="/start")
    h.message(text=texts.MENU_SETTINGS, message_id=10)
    h.press_text(texts.SET_TZ_LABEL)
    h.press_text("След. →")
    h.press_text("← Пред.")
    h.message(text="Берлин", message_id=11)
    label = next(label for label in h.last_labels() if "Берлин" in label)
    h.press_text(label)
    assert services.settings.get().timezone == "Europe/Berlin"
    assert h.last_text().startswith(texts.TITLE_SETTINGS)
    assert len(h.sent()) == 8
    assert 10 not in h.session.visible and 11 not in h.session.visible
    assert h.ui.has_effect(f"{OWNER}:11")
    assert list(h.session.visible)[-1] == h.card_id()
    assert len(h.session.visible) == 2
