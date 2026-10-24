"""Route actual rendered persistent keys as Telegram text updates, never callbacks."""
import pytest

from poster_bot.ui.state import State
from poster_bot.ui import texts
from test_ui_handlers_offline import OWNER, _Harness
from test_ui_navigation import callback


@pytest.mark.parametrize("index,state,view", [
    (0, State.CREATING, None),
    (1, State.MENU, "drafts"),
    (2, State.MENU, "day"),
    (3, State.SETTINGS, None),
    (4, State.MENU, "journal"),
    (5, State.MENU, "day"),
])
@pytest.mark.parametrize("origin", ["home", "settings", "intake", "time"])
def test_rendered_bottom_key_routes_as_text(services, index, state, view, origin):
    h = _Harness(services)
    h.message(text="/start")
    keys = [button["text"] for row in h.sent()[0]["reply_markup"]["keyboard"]
            for button in row]
    # Establish the working inline route before exercising the distinct text route.
    callback(h, h._button_data("Создать пост"), h.card_id())
    assert h.ui.get().state is State.CREATING
    h.message(text="/start", message_id=40)
    if origin == "settings":
        h.message(text=keys[3], message_id=42)
    elif origin in ("intake", "time"):
        callback(h, h._button_data("Создать пост"), h.card_id())
        if origin == "time":
            h.message(text="isolated test post", message_id=43)
            callback(h, h._button_data(texts.BTN_CONTINUE), h.card_id())
            assert h.ui.get().state is State.DRAFT
            assert h.ui.get().arg("schedule_picker") is True
    posts = services.drafts.list_drafts()
    h.message(text=keys[index], message_id=41)
    assert h.ui.get().state is state
    if view:
        assert h.ui.get().arg("view") == view
    else:
        assert not (h.ui.get().arg("staged") or {}).get("kind")
    assert services.drafts.list_drafts() == posts
    assert not h.ui.has_effect(f"{OWNER}:41")  # menu text never enters intake
    assert 41 not in h.session.visible


@pytest.mark.parametrize("index", range(6))
@pytest.mark.parametrize("user,chat,kind", [
    (999, OWNER, "private"), (OWNER, -999, "supergroup"),
    (OWNER, 999, "private"),
])
def test_rendered_bottom_key_guard_before_effects(services, index, user, chat, kind):
    h = _Harness(services)
    h.message(text="/start")
    keys = [button["text"] for row in h.sent()[0]["reply_markup"]["keyboard"]
            for button in row]
    before_requests, before_state = list(h.session.requests), h.ui.get()
    h.message(text=keys[index], message_id=41,
              chat={"id": chat, "type": kind},
              **{"from": {"id": user, "is_bot": False, "first_name": "x"}})
    assert h.session.requests == before_requests
    assert h.ui.get() == before_state
    assert services.drafts.list_drafts() == []
