"""Adapter tests: guards, mapping helpers, offline dispatch via Dispatcher.

No network: a real aiogram Bot runs over a fake session that records requests
and replays canned Telegram responses through aiogram's normal parsing path.
"""
from __future__ import annotations

import asyncio
import json
import pytest
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, Update

from conftest import NOW, Services, text_content
from ui_flow_helpers import FLOW_CHANNEL, build_flow
from poster_bot.ui import handlers
from poster_bot.ui.replies import TEXT_MAX, Button, Reply
from poster_bot.ui.staged import Staged
from poster_bot.ui.state import State, UiStateRepo
import poster_bot.ui.texts as texts

OWNER = 111
_DATE_ISO = datetime.fromtimestamp(NOW, tz=timezone.utc).isoformat()


def _msg(**kwargs: Any) -> Message:
    base: dict[str, Any] = {
        "message_id": 1,
        "date": _DATE_ISO,
        "chat": {"id": OWNER, "type": "private"},
        "from": {"id": OWNER, "is_bot": False, "first_name": "Владелец"},
    }
    base.update(kwargs)
    return Message.model_validate(base)


def _callback(data: str, chat_id: int = OWNER, user_id: int = OWNER) -> CallbackQuery:
    return CallbackQuery.model_validate(
        {
            "id": "cb-1",
            "from": {"id": user_id, "is_bot": False, "first_name": "x"},
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": 2,
                "date": _DATE_ISO,
                "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "channel"},
                "text": "меню",
            },
        }
    )


# ---------------------------------------------------------------- guards


def test_guard_message_owner_private() -> None:
    assert handlers.message_allowed(_msg(text="ok"), owner_id=OWNER)
    assert not handlers.message_allowed(_msg(text="no"), owner_id=999)
    group = _msg(text="no", chat={"id": -100999, "type": "group"})
    assert not handlers.message_allowed(group, owner_id=OWNER)


def test_guard_message_anonymous_or_service() -> None:
    anonymous = Message.model_validate(
        {
            "message_id": 1,
            "date": _DATE_ISO,
            "chat": {"id": OWNER, "type": "private"},
            "text": "no",
            "sender_chat": {"id": OWNER, "type": "channel"},
        }
    )
    assert anonymous.from_user is None
    assert not handlers.message_allowed(anonymous, owner_id=OWNER)
    service = Message.model_validate(
        {
            "message_id": 3,
            "date": _DATE_ISO,
            "chat": {"id": OWNER, "type": "private"},
            "new_chat_members": [{"id": 5, "is_bot": False, "first_name": "x"}],
        }
    )
    # service messages carry no from_user: dropped as unauthorised, see handlers.py
    assert not handlers.message_allowed(service, owner_id=OWNER)


def test_guard_callback() -> None:
    assert handlers.callback_allowed(_callback("a123"), owner_id=OWNER)
    assert not handlers.callback_allowed(_callback("a123", user_id=5), owner_id=OWNER)
    assert not handlers.callback_allowed(_callback("a123", chat_id=-100777), owner_id=OWNER)


# ------------------------------------------------------- mapping helpers


def _entities(message: Message):
    return handlers.to_domain_entities(message.entities)


def test_entities_mapped_verbatim() -> None:
    msg = _msg(
        text="Смотри @dude и 🔥 с цитатой",
        entities=[
            {"type": "mention", "offset": 7, "length": 5},
            {"type": "custom_emoji", "offset": 15, "length": 2,
             "custom_emoji_id": "7"},
            {"type": "text_mention", "offset": 20, "length": 3,
             "user": {"id": 42, "is_bot": False, "first_name": "Лена",
                      "username": "lena"}},
        ],
    )
    mapped = _entities(msg)
    assert [e.type for e in mapped] == ["mention", "custom_emoji", "text_mention"]
    assert mapped[0].offset == 7 and mapped[0].length == 5
    assert mapped[0].user_id is None and mapped[0].username is None
    assert mapped[1].custom_emoji_id == "7"
    assert mapped[2].user_id == 42 and mapped[2].username == "lena"


def test_entities_pre_language_and_empty() -> None:
    msg = _msg(
        text="print(1)",
        entities=[{"type": "pre", "offset": 0, "length": 8, "language": "python"}],
    )
    mapped = _entities(msg)
    assert mapped[0].language == "python"
    assert handlers.to_domain_entities(None) == ()


def test_forward_source_uses_received_copy() -> None:
    fwd = _msg(
        text="пост",
        forward_origin={
            "type": "channel", "date": NOW,
            "chat": {"id": -100555, "type": "channel", "title": "Крутой"},
            "message_id": 77, "author_signature": "Автор канала",
        },
    )
    # re-forward happens from the copy the bot received (owner chat), not from
    # the original channel — the bot has guaranteed access to it
    assert handlers._forward_source(fwd) == (OWNER, 1)
    assert handlers._forward_source(_msg(text="обычное")) is None
    hidden = _msg(text="пост", forward_origin={"type": "hidden_user", "date": NOW,
                                               "sender_user_name": "Аноним"})
    assert handlers._forward_source(hidden) == (OWNER, 1)


def test_copy_candidate_photo_and_text() -> None:
    photo = _msg(
        caption="Фото-подпись",
        photo=[{"file_id": "s", "file_unique_id": "us", "width": 90, "height": 90},
               {"file_id": "l", "file_unique_id": "ul", "width": 800, "height": 600}],
        caption_entities=[{"type": "bold", "offset": 0, "length": 4}],
    )
    staged = handlers._copy_candidate(photo)
    assert staged.kind == "media"
    assert staged.media is not None
    assert staged.media.kind == "photo" and staged.media.file_id == "l"
    assert staged.media.caption == "Фото-подпись"
    assert staged.media.caption_entities[0].type == "bold"
    text_staged = handlers._copy_candidate(_msg(text="просто текст"))
    assert text_staged.kind == "text" and text_staged.text == "просто текст"
    assert handlers._copy_candidate(
        _msg(voice={"file_id": "v", "file_unique_id": "vu", "duration": 3})
    ).kind is None


def test_keyboard_token_and_url_buttons() -> None:
    reply = Reply("текст", (
        (Button("Кнопка", "atoken123"),),
        (Button("Ссылка", url="https://t.me/c/555/9"),),
    ))
    markup = handlers._keyboard(reply)
    assert isinstance(markup, InlineKeyboardMarkup)
    first = markup.inline_keyboard[0][0]
    assert first.callback_data == "atoken123" and first.url is None
    second = markup.inline_keyboard[1][0]
    assert second.url == "https://t.me/c/555/9" and second.callback_data is None
    assert handlers._keyboard(Reply("пусто", ())) is None


def test_chunks_respect_4096_limit() -> None:
    assert handlers._chunks("короткий") == ["короткий"]
    huge = "А" * (TEXT_MAX + 904)
    parts = handlers._chunks(huge)
    assert len(parts) == 2
    assert all(len(p) <= TEXT_MAX for p in parts)
    assert sum(len(p) for p in parts) == len(huge)
    lines = ["B" * 3000, "C" * 3000, "D" * 10]
    parts = handlers._chunks("\n".join(lines))
    assert all(len(p) <= TEXT_MAX for p in parts)
    assert "".join(parts).count("B") == 3000 and "".join(parts).count("C") == 3000


# ------------------------------------------------------------ dispatch


class _FakeApiSession(BaseSession):
    """Records outgoing methods; replies with canned payloads through
    aiogram's normal response-parsing path."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 900
        self.edit_error: str | None = None
        self.answer_error: str | None = None
        self.send_error: str | None = None
        self.visible: dict[int, dict[str, Any]] = {}
        self.before_delete = None
        self.before_answer = None

    async def close(self) -> None:
        pass

    async def stream_content(self, url, headers=None, timeout=30,
                             chunk_size=65536, raise_for_status=True):
        raise AssertionError("no file downloads in offline tests")
        yield b""  # pragma: no cover

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        api_method = method.__api_method__
        payload = method.model_dump(
            mode="json", exclude_none=True, exclude={"default"},
            fallback=lambda obj: str(obj),
        )
        self.requests.append((api_method, payload))
        if api_method == "answerCallbackQuery" and self.before_answer:
            self.before_answer()
        if api_method == "sendMessage" and self.send_error:
            error, self.send_error = self.send_error, None
            raise TelegramBadRequest(method=method, message=error)
        if api_method == "answerCallbackQuery" and self.answer_error:
            error, self.answer_error = self.answer_error, None
            raise TelegramBadRequest(method=method, message=error)
        if api_method == "editMessageText" and self.edit_error:
            error, self.edit_error = self.edit_error, None
            raise TelegramBadRequest(method=method, message=error)
        if api_method in {"answerCallbackQuery", "deleteMessage"}:
            raw: Any = True
        elif api_method == "sendMediaGroup":
            raw = [self._raw(), self._raw()]
        elif api_method in {"forwardMessages", "copyMessages"}:
            raw = [{"message_id": self._raw()["message_id"]}
                   for _ in payload["message_ids"]]
        elif api_method == "editMessageText":
            raw = {"message_id": payload["message_id"], "date": NOW,
                   "chat": {"id": payload["chat_id"], "type": "private"},
                   "text": payload["text"]}
        else:
            raw = self._raw()
        if api_method == "deleteMessage":
            if self.before_delete:
                self.before_delete(payload["message_id"])
            self.visible.pop(payload["message_id"], None)
        elif api_method == "editMessageText":
            # Local history clearing is invisible to the server: editing still
            # succeeds, but cannot recreate a locally hidden message.
            if payload["message_id"] in self.visible:
                self.visible[payload["message_id"]] = payload
        elif api_method == "sendMessage":
            self.visible[raw["message_id"]] = payload
        content = json.dumps({"ok": True, "result": raw})
        response = self.check_response(bot=bot, method=method, status_code=200,
                                       content=content)
        return response.result

    def _raw(self) -> dict[str, Any]:
        message_id = self._next_id
        self._next_id += 1
        return {
            "message_id": message_id,
            "date": NOW,
            "chat": {"id": OWNER, "type": "private"},
        }


class _Harness:
    def __init__(self, services: Services) -> None:
        self.services = services
        self.session = _FakeApiSession()
        self.bot = Bot("42:TEST", session=self.session)
        self.flow, self.preview = build_flow(services)
        from poster_bot.ui.dashboard_presentation import DashboardPresentation
        self.dashboard = DashboardPresentation(self.flow, owner_id=OWNER)
        self.dispatcher = Dispatcher(storage=MemoryStorage())
        self.now = NOW
        self.dispatcher.include_router(
            handlers.create_ui_router(self.flow, owner_id=OWNER, clock=lambda: self.now,
                                      dashboard=self.dashboard)
        )
        self.ui = UiStateRepo(services.conn)
        self._update_id = 0

    def feed(self, content: dict[str, Any]) -> None:
        self._update_id += 1
        update = {"update_id": self._update_id, **content}
        asyncio.run(self.dispatcher.feed_update(self.bot, Update.model_validate(update)))

    def message(self, **kwargs: Any) -> None:
        incoming = _raw_message(**kwargs)
        self.session.visible[incoming["message_id"]] = incoming
        self.feed({"message": incoming})

    def press_text(self, label: str, *, user_id: int = OWNER) -> None:
        if label in texts.MENU_ALIASES:
            self.message(text=label, message_id=10000 + self._update_id,
                         **{"from": {"id": user_id, "is_bot": False, "first_name": "x"}})
            return
        data = self._button_data(label)
        self.feed({"callback_query": {
            "id": f"cb{self._update_id}",
            "from": {"id": user_id, "is_bot": False, "first_name": "x"},
            "chat_instance": "ci",
            "data": data,
            "message": _raw_message(message_id=self.card_id(), text="меню"),
        }})

    def _button_data(self, label: str) -> str:
        for method, payload in reversed(self.session.requests):
            if method not in {"sendMessage", "editMessageText"}:
                continue
            for row in payload.get("reply_markup", {}).get("inline_keyboard", []):
                for button in row:
                    if button["text"] == label:
                        return button["callback_data"]
        raise AssertionError(f"no button {label!r} in sent messages")

    def sent(self) -> list[dict[str, Any]]:
        return [p for m, p in self.session.requests if m == "sendMessage"]

    def last_text(self) -> str:
        return self.cards()[-1]["text"]

    def cards(self) -> list[dict[str, Any]]:
        return [p for m, p in self.session.requests
                if m in {"sendMessage", "editMessageText"}]

    def card_id(self) -> int:
        return self.flow.navigation_store(OWNER).get(OWNER)

    def last_labels(self) -> list[str]:
        markup = self.cards()[-1].get("reply_markup", {})
        return [b["text"] for row in markup.get("inline_keyboard", []) for b in row]


def _raw_message(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "message_id": 1,
        "date": NOW,
        "chat": {"id": OWNER, "type": "private"},
        "from": {"id": OWNER, "is_bot": False, "first_name": "Владелец"},
    }
    base.update(kwargs)
    return base


def _private_text(text: str) -> dict[str, Any]:
    return {"message": _raw_message(text=text)}


@pytest.mark.parametrize("purpose", ["schedule", "edit_time", "replan"])
@pytest.mark.parametrize("answer_error", [None, "query is too old"])
@pytest.mark.parametrize("after_ack", [3599, 3600, 3602])
def test_confirmation_rechecks_clock_after_ack(services, purpose, answer_error, after_ack):
    from conftest import future, text_content

    h = _Harness(services)
    post = services.drafts.create(text_content(), now_epoch=NOW)
    old = None
    if purpose != "schedule":
        old = services.publications.schedule(post, future(minutes=120), now_epoch=NOW)
        if purpose == "replan":
            services.conn.execute("UPDATE publications SET status='missed' WHERE id=?", (old.id,))
            services.conn.commit()
            old = services.publications.get(old.id)
    h.message(text="/start")
    h.press_text(texts.MENU_DRAFTS if old is None else texts.MENU_PLAN)
    if old is None:
        h.press_text(next(label for label in h.last_labels()
                          if label.startswith(f"№{post} |")))
    else:
        marker = "❌" if purpose == "replan" else "🕒"
        h.press_text(next(label for label in h.last_labels()
                          if label == f"13:00 | {marker} | hello owner"))
    h.press_text(texts.BTN_SCHEDULE if old is None else
                 texts.BTN_EDIT_TIME if purpose == "edit_time" else texts.BTN_REPLAN)
    h.message(text="12:00", message_id=40)
    assert h.last_text() == '❗ Публикация будет запланирована на 12:00 21.10.2026 ❗'
    assert services.publications.get_by_post(post) == old
    assert h.last_labels() == [texts.BTN_CONFIRM, "Изменить дату", texts.BTN_CANCEL]
    h.now = NOW + 3599  # 11:59:59, before the selected minute
    h.session.before_answer = lambda: setattr(h, "now", NOW + after_ack)
    h.session.answer_error = answer_error
    before = len(h.session.requests)
    h.press_text(texts.BTN_CONFIRM)
    assert sum(m == "answerCallbackQuery" for m, _ in h.session.requests[before:]) == 1
    if after_ack < 3600:
        assert texts.SCHEDULED_PREFIX in h.last_text()
        assert services.publications.get_by_post(post).minute_epoch == NOW + 3600
        return
    assert texts.TIME_NOT_FUTURE in h.last_text()
    assert "12:00" in h.last_text() and "21.10.2026" in h.last_text()
    assert h.last_labels() == ["Изменить дату", texts.BTN_BACK]
    assert h.ui.get().state is State.MANUAL_TIME
    assert h.ui.get().arg("time_form") == {
        "date": "2026-10-21", "time": "12:00", "field": None}
    assert services.publications.get_by_post(post) == old
    assert (NOW + 3600) // 60 not in services.publications.reserved_minutes()
    assert services.manual_times.recent() == []


def test_date_selection_rechecks_clock_after_ack(services):
    h = _Harness(services)
    h.message(text="/start")
    h.press_text(texts.MENU_NEW_POST)
    h.message(text="post", message_id=40)
    h.press_text(texts.BTN_CONTINUE)
    h.message(text="12:00", message_id=41)
    assert texts.BTN_CONFIRM in h.last_labels()
    h.press_text("Изменить дату")
    h.now = NOW + 3599
    h.session.before_answer = lambda: setattr(h, "now", NOW + 3602)
    before = len(h.session.requests)
    h.press_text("Сегодня")
    assert sum(m == "answerCallbackQuery" for m, _ in h.session.requests[before:]) == 1
    assert texts.TIME_NOT_FUTURE in h.last_text()
    assert "12:00" in h.last_text()
    assert texts.BTN_CONFIRM not in h.last_labels()
    assert "Продолжить" not in h.last_labels()
    assert services.publications.get_by_post(h.ui.get().arg("post_id")) is None
    assert services.publications.reserved_minutes() == set()


@pytest.mark.parametrize("expired", [True, False])
def test_hour_selection_rechecks_clock_after_ack(services, expired):
    h = _Harness(services)
    h.message(text="/start")
    h.press_text(texts.MENU_NEW_POST)
    h.message(text="post", message_id=40)
    h.press_text(texts.BTN_CONTINUE)
    h.press_text(texts.BTN_DAY_SLOTS)
    h.now = NOW + 3599
    h.session.before_answer = lambda: setattr(h, "now", NOW + (3602 if expired else 3599))
    h.press_text("12:00")
    if expired:
        assert "Этот час уже недоступен" in h.last_text()
        assert "12:00" not in h.last_labels()
        assert texts.BTN_CONFIRM not in h.last_labels()
    else:
        assert h.last_text() == '❗ Публикация будет запланирована на 12:00 21.10.2026 ❗'
        h.press_text(texts.BTN_CONFIRM)
        assert texts.SCHEDULED_PREFIX in h.last_text()


def test_dispatch_start_sends_menu(services: Services) -> None:
    h = _Harness(services)
    h.feed(_private_text("/start"))
    assert [[button["text"] for button in row]
            for row in h.cards()[-1]["reply_markup"]["inline_keyboard"]] == [
                ["← Пред.", "Сегодня", "След. →"], ["Создать пост"]]
    assert texts.BTN_BACK not in h.last_labels()
    assert any(button["text"] == texts.MENU_NEW_POST
               for row in h.sent()[0]["reply_markup"]["keyboard"] for button in row)
    assert h.ui.get().state is State.MENU


@pytest.mark.parametrize("label", ["Отложенное", "Черновики"])
def test_postponed_section_new_and_legacy_keyboard_route(services, label):
    post_id = services.drafts.create(text_content("stored post"), now_epoch=NOW)
    h = _Harness(services)
    h.message(text="/start")
    keyboard = h.sent()[0]["reply_markup"]["keyboard"]
    keys = [button["text"] for row in keyboard for button in row]
    assert len(keys) == 6 and texts.MENU_DRAFTS in keys and "Черновики" not in keys
    h.message(text=label, message_id=50)
    assert h.last_text().startswith("Отложенное")
    assert "Чернов" not in h.last_text()
    h.press_text(next(value for value in h.last_labels() if value.startswith(f"№{post_id} |")))
    assert "Отложенный пост" in h.last_text() and "Чернов" not in h.last_text()
    assert services.drafts.get_content(post_id).text == "stored post"
    before = list(h.session.requests)
    h.message(text=label, message_id=51,
              **{"from": {"id": 999, "is_bot": False, "first_name": "x"}})
    h.message(text=label, message_id=52,
              chat={"id": -100999, "type": "supergroup"})
    assert h.session.requests == before


def test_dispatch_ignores_non_owner_silently(services: Services) -> None:
    h = _Harness(services)
    h.message(text="привет", **{"from": {"id": 999, "is_bot": False, "first_name": "Чужой"}})
    assert h.session.requests == []
    assert h.ui.get().version == 0
    assert services.drafts.list_drafts() == []


def test_dispatch_ignores_group_message(services: Services) -> None:
    h = _Harness(services)
    h.message(text="/start", chat={"id": -100999, "type": "supergroup"})
    assert h.session.requests == []


def test_dispatch_service_message_silent(services: Services) -> None:
    h = _Harness(services)
    # service messages without a sender never pass the owner guard
    h.feed({"message": {
        "message_id": 4,
        "date": NOW,
        "chat": {"id": OWNER, "type": "private"},
        "new_chat_members": [{"id": 5, "is_bot": False, "first_name": "x"}],
    }})
    assert h.session.requests == []


def test_dispatch_forged_token_stale_reply(services: Services) -> None:
    h = _Harness(services)
    h.feed(_private_text("/start"))
    before = len(h.session.requests)
    card = h.cards()[-1]
    state = h.ui.get()
    h.feed({"callback_query": {
        "id": "cbx",
        "from": {"id": OWNER, "is_bot": False, "first_name": "x"},
        "chat_instance": "ci",
        "data": "a" + "0" * 12,
        "message": _raw_message(message_id=h.card_id(), text="меню"),
    }})
    assert [m for m, _ in h.session.requests[before:]] == ["answerCallbackQuery"]
    assert h.session.requests[-1][1]["text"] == texts.BUTTON_STALE
    assert h.cards()[-1] == card
    # чужого действия не произошло: состояние всё ещё меню
    assert h.ui.get().state is State.MENU
    assert h.ui.get() == state


def test_dispatch_callback_from_other_user_never_executes(services: Services) -> None:
    h = _Harness(services)
    h.feed(_private_text("/start"))
    # A callback issued by the old main card remains valid after the UI move.
    token = h.flow._token(h.flow.A_JOURNAL, now=NOW)
    before = len(h.sent())
    h.feed({"callback_query": {
        "id": "cbo",
        "from": {"id": 999, "is_bot": False, "first_name": "Чужой"},
        "chat_instance": "ci",
        "data": token,
        "message": _raw_message(message_id=h.card_id(), text="меню"),
    }})
    # чужое нажатие молча отвергнуто, токен не израсходован
    assert len(h.sent()) == before
    assert h.ui.get().state is State.MENU
    # владелец той же кнопкой пользуется
    h.feed({"callback_query": {
        "id": "cbo2",
        "from": {"id": OWNER, "is_bot": False, "first_name": "x"},
        "chat_instance": "ci",
        "data": token,
        "message": _raw_message(message_id=h.card_id(), text="меню"),
    }})
    assert h.ui.get().payload == {"view": "journal"}


@pytest.mark.parametrize("signed", [False, True])
def test_dispatch_photo_creates_draft_and_preview_only_to_owner(services: Services, signed) -> None:
    h = _Harness(services)
    if signed:
        from poster_bot.domain.signature import SignatureConfig
        from poster_bot.repo.plans import PlanStore

        h.flow._signature = SignatureConfig(
            "Main", "https://example.org/main", "Partner", "https://example.org/partner")
        h.flow._plans = PlanStore(services.conn)
    h.feed(_private_text("/start"))
    h.press_text("Создать пост")
    h.message(message_id=5,
              photo=[{"file_id": "s", "file_unique_id": "us", "width": 90, "height": 90},
                     {"file_id": "l", "file_unique_id": "ul", "width": 800, "height": 600}],
              caption="Фото-подпись")
    assert services.drafts.list_drafts() == []  # черновик создаётся по «Продолжить»
    # Signed intake renders immediately through central presentation; legacy
    # unsigned intake first renders when Continue creates the durable draft.
    assert [c.kind for c in h.preview.owner_previews] == (["photo"] if signed else [])
    assert h.last_labels() == [texts.BTN_CONTINUE, texts.BTN_CANCEL]
    if signed:
        staged_preview = h.preview.owner_previews[0]
        assert staged_preview.media.caption == "Фото-подпись\n\nMain | Partner"
        assert staged_preview.media.file_id == "l"
    h.press_text(texts.BTN_CONTINUE)
    post_ids = services.drafts.list_drafts()
    assert len(post_ids) == 1
    content = services.drafts.get_content(post_ids[0])
    assert content.kind == "photo"
    assert content.media is not None
    assert content.media.caption == ("Фото-подпись\n\nMain | Partner" if signed else "Фото-подпись")
    assert content.media.file_id == "l"
    # предпросмотр ушёл владельцу, и только ему
    assert [c.kind for c in h.preview.owner_previews] == ["photo"]
    assert h.preview.owner_previews == [content]
    if signed:
        saved = h.flow._plans.get(post_ids[0])
        assert [part.content for part in saved.plan.parts] == [content]
        assert saved.logical.body == "Фото-подпись"
    assert h.preview.calls == []  # в канал ничего не публиковалось
    assert all(payload.get("chat_id", OWNER) == OWNER
               for _, payload in h.session.requests)
    assert texts.PICK_TIME in h.last_text()
    assert texts.BTN_OWN_TIME not in h.last_labels()
    assert texts.BTN_PREVIEW not in h.last_labels()
    assert h.ui.get().arg("post_id") == post_ids[0]
    assert all(m != "sendPhoto" for m, _ in h.session.requests)
    assert all(len(p["text"]) <= TEXT_MAX for p in h.sent())


def test_dispatch_album_buffers_until_continue(services: Services) -> None:
    h = _Harness(services)
    h.feed(_private_text("/start"))
    h.press_text("Создать пост")
    h.message(message_id=6, media_group_id="grp1",
              photo=[{"file_id": "p1", "file_unique_id": "u1", "width": 800,
                      "height": 600}])
    h.message(message_id=7, media_group_id="grp1",
              video={"file_id": "v1", "file_unique_id": "v1u", "width": 640,
                     "height": 480, "duration": 12})
    assert services.drafts.list_drafts() == []  # до «Продолжить» черновика нет
    staged = Staged.from_dict(h.ui.get().arg("staged"))
    assert staged.kind == "album" and staged.album_group_id == "grp1"
    assert [m.kind for m in staged.album_items] == ["photo", "video"]


def test_dispatch_forward_shows_choice(services: Services) -> None:
    h = _Harness(services)
    h.feed(_private_text("/start"))
    h.press_text("Создать пост")
    h.message(message_id=8, text="чужой пост",
              forward_origin={"type": "channel", "date": NOW,
                              "chat": {"id": -100555, "type": "channel"},
                              "message_id": 77})
    assert texts.FORWARD_ASK.split("\n")[0] in h.last_text()
    labels = [b["text"] for row in h.cards()[-1]["reply_markup"]["inline_keyboard"]
              for b in row]
    assert texts.BTN_FORWARD_AS_IS in labels and texts.BTN_MAKE_COPY in labels
    staged = Staged.from_dict(h.ui.get().arg("staged"))
    assert staged.kind == "forward"


def test_dispatch_unsupported_content(services: Services) -> None:
    h = _Harness(services)
    h.feed(_private_text("/start"))
    h.message(message_id=9,
              voice={"file_id": "st", "file_unique_id": "stu", "duration": 3})
    assert h.last_text().startswith(texts.UNSUPPORTED.split("\n")[0])
