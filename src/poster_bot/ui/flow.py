"""The owner conversation engine: every owner-facing decision, validated
against durable state, with Russian replies and token-issued buttons.

Transport-free by design: the aiogram adapter normalizes updates, enforces
the owner/private guard, and calls these methods; tests drive the same
methods directly. No method here trusts a stale step: the durable
conversation version, the publication row version, "strictly future" and
minute uniqueness are re-checked at execution inside the domain services.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from ..domain.content import Content, ContentValidationError, Entity
from ..domain.errors import (
    ConflictError,
    DomainError,
    MinuteTakenError,
    NonLocalTimeError,
    AmbiguousLocalTimeError,
    PastTimeError,
    TransitionError,
)
from ..domain.states import Status
from ..domain.time import Instant, recent_manual_suggestions
from ..journal import Journal
from ..repo.drafts import DraftNotFound, DraftStore
from ..repo.manual_times import ManualTimesRepo
from ..repo.publications import Publication, PublicationsService
from ..repo.dispatch import DispatchService
from ..repo.settings import (
    KEY_END,
    KEY_START,
    KEY_TZ,
    SettingsError,
    SettingsRepo,
)
from ..transport.ports import DefinitiveFailure, PublishOutcome, Sent, UncertainOutcome
from .actions import Action, ActionStore, ActionStale
from .replies import Button, DAY_PAGE_SIZE, MAX_ROWS, PAGE_SIZE, Reply, nav_rows, page_window
from .state import (
    EffectAlreadyApplied,
    StaleStateError,
    State,
    UiState,
    UiStateRepo,
)
from .staged import (
    Staged,
    copy_candidate_from_media,
    copy_candidate_from_text,
    entities_from_json,
    entities_to_json,
)
from .times_input import (
    day_bounds,
    format_local,
    local_day,
    parse_clock,
    resolve_manual_time,
)
from . import texts
from . import timezones
from .navigation import NavigationStore
from .scheduling import SchedulingCards, CARD_ACTIONS
from .dashboard import Dashboard, snapshot as dashboard_snapshot, summary

PREVIEW_FAIL_RU = {
    "timeout": "таймаут канала",
    "transport_error": "сбой связи с каналом",
    "partial_result": "канал вернул не всё содержимое",
    "api_error": "канал отверг сообщение",
    "rate_limited": "канал ограничил частоту",
}

CAPTION_AMBIGUOUS_RU = (
    "Альбом уже содержит подписи отдельных элементов: заменить их одной "
    "подписью альбома нельзя. Замените содержимое целиком или оставьте как есть."
)

CODE_RU = {
    "published": "опубликован",
    "definitive_failure": "канал отверг",
    "too_early": "слишком рано",
    "past_due": "слот истёк",
    "in_flight": "уже отправляется",
    "not_scheduled": "не запланирован",
    "version_stale": "устаревшая версия",
    "timeout": "таймаут",
    "transport_error": "сбой связи",
    "partial_result": "неполный результат",
    "api_error": "ошибка канала",
    "rate_limited": "лимит частоты",
    "slot_start_hour": "час начала",
    "slot_end_hour": "час конца",
    "timezone": "часовой пояс",
}


class OwnerPreview(Protocol):
    """Deliver preview content to the OWNER privately. The channel-bound
    publisher stays pointed at the channel: previews never retarget it."""

    async def send_preview(self, content: Content) -> PublishOutcome: ...


class OwnerFlow(SchedulingCards):
    # action kinds (server-side only; callback data carries the token alone)
    A_MENU = "menu"
    A_NEW = "new_post"
    A_CONTINUE = "continue"
    A_REPLACE_YES = "replace_yes"
    A_REPLACE_KEEP = "replace_keep"
    A_CAPTION = "caption_set"
    A_FORWARD_AS_IS = "forward_as_is"
    A_FORWARD_COPY = "forward_copy"
    A_DRAFTS = "drafts"
    A_DRAFT_OPEN = "draft_open"
    A_DRAFT_SELECT = "draft_select"
    A_DRAFT_PREVIEW = "draft_preview"
    A_DRAFT_EDIT = "draft_edit"
    A_DRAFT_DELETE = "draft_delete"
    A_DRAFT_DELETE_YES = "draft_delete_yes"
    A_DRAFT_SCHEDULE = "draft_schedule"
    A_PLAN = "plan"
    A_DAY = "day"
    A_PUB_OPEN = "pub_open"
    A_PUB_PREVIEW = "pub_preview"
    A_PUB_PAUSE = "pub_pause"
    A_PUB_EDIT_CONTENT = "pub_edit_content"
    A_PUB_EDIT_TIME = "pub_edit_time"
    A_PUB_RESUME = "pub_resume"
    A_PUB_CANCEL = "pub_cancel"
    A_PUB_CANCEL_YES = "pub_cancel_yes"
    A_PUB_DELETE = "pub_delete"
    A_PUB_DELETE_YES = "pub_delete_yes"
    A_PUB_REPLAN = "pub_replan"
    A_MARK_PUBLISHED = "mark_published_ask"
    A_TO_DRAFT_RISK = "to_draft_risk_ask"
    A_ACK_RISK = "to_draft_risk_ack"
    A_SCHED_NEXT = "sched_next"
    A_SCHED_OWN = "sched_own"
    A_SCHED_RECENT = "sched_recent"
    A_CONFIRM = "confirm_schedule"
    A_CONFIRM_TIME_EDIT = "confirm_time_edit"
    A_CONFIRM_REPLAN = "confirm_replan"
    A_SETTINGS = "settings"
    A_SET_TZ = "settings_tz"
    A_SET_START = "settings_start"
    A_SET_END = "settings_end"
    A_JOURNAL = "journal"
    A_BACK = "back"

    def __init__(
        self,
        *,
        conn,
        settings: SettingsRepo,
        drafts: DraftStore,
        publications: PublicationsService,
        dispatch: DispatchService,
        manual_times: ManualTimesRepo,
        journal: Journal,
        preview: OwnerPreview,
        channel_id: int,
        channel_username: str | None = None,
        state: UiStateRepo | None = None,
        actions: ActionStore | None = None,
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._drafts = drafts
        self._publications = publications
        self._dispatch = dispatch
        self._manual_times = manual_times
        self._journal = journal
        self._preview = preview
        # resolved numeric destination (a configured @username arrives already
        # resolved); the bare username only builds correct public t.me links
        if isinstance(channel_id, bool) or not isinstance(channel_id, int):
            raise ValueError("channel_id must be a resolved numeric chat id")
        self._channel_id = channel_id
        username = (channel_username or "").strip().removeprefix("@").strip()
        self._channel_username = username or None
        self._state = state or UiStateRepo(conn)
        self._actions = actions or ActionStore(conn)

    # ------------------------------------------------------------------ core
    def navigation_store(self, owner_id: int) -> NavigationStore:
        return NavigationStore(self._conn, owner_id)

    def input_captured(self, source_key: str) -> bool:
        """A durable intake receipt, not merely a successful UI response."""
        return self._state.has_effect(source_key)

    async def menu_navigation(self, *, label: str, now: int) -> Reply:
        """Stable reply-keyboard commands, independent of consumed tokens."""
        if label == texts.MENU_NEW_POST:
            return await self.new_post(now=now)
        if label in (texts.MENU_DRAFTS, texts.LEGACY_MENU_DRAFTS):
            return self.drafts_view(now=now, page=0)
        if label == texts.MENU_PLAN:
            return self.plan_today(now=now)
        if label == texts.MENU_SETTINGS:
            return self.settings_view(now=now)
        return await self.command_start(now=now)

    def _ui(self) -> UiState:
        return self._state.get()

    def _go(self, state: State, payload: dict, *, now: int,
            expected_version: int | None = None) -> UiState:
        return self._state.set(state, payload, expected_version=expected_version,
                               now_epoch=now)

    def _staged(self, ui: UiState) -> Staged:
        data = ui.arg("staged")
        return Staged.from_dict(data) if isinstance(data, dict) else Staged()

    def _staged_payload(self, staged: Staged, ui: UiState) -> dict:
        payload = dict(ui.payload)
        payload.pop("pending_replace", None)
        payload.pop("pending_caption", None)
        payload["staged"] = staged.to_dict()
        return payload

    def _token(self, action: str, *, now: int, **kw) -> str:
        return self._actions.create(action, now_epoch=now, **kw)

    def _btn(self, label: str, action: str, *, now: int, **kw) -> Button:
        if action in (self.A_SCHED_NEXT, self.A_SCHED_OWN, self.A_SCHED_RECENT):
            kw.setdefault('expected_version', self._ui().version)
        return Button(label, self._token(action, now=now, **kw))

    def _menu_reply(self, now: int) -> Reply:
        ui = self._ui()
        if ui.state is not State.MENU or ui.payload:
            # Intake replay/error replies must remain neutral and must not
            # impersonate a main dashboard over a draft or an unfinished form.
            return Reply(texts.MENU_TEXT, self._menu_rows(now))
        return self.dashboard_reply(now=now, snapshot=self.dashboard_snapshot())

    def dashboard_snapshot(self) -> Dashboard:
        return dashboard_snapshot(self._publications, self._dispatch,
                                  self._settings.get().timezone)

    def dashboard_current(self, version: int) -> bool:
        ui = self._ui()
        return ui.state is State.MENU and not ui.payload and ui.version == version

    def dashboard_reply(self, *, now: int, snapshot: Dashboard) -> Reply:
        """Render without resetting the conversation; only changed frames issue tokens."""
        entries = [self._btn(
            entry.label, self.A_PUB_OPEN, now=now,
            publication_id=entry.publication_id, expected_version=entry.version,
            arg=f'row:{self._ui().version}',
        ) for entry in snapshot.entries]
        rows = [(entry,) for entry in entries]
        rows.extend(self._menu_rows(now))
        return Reply(snapshot.text, tuple(rows), dashboard=snapshot,
                     dashboard_version=self._ui().version)

    def _menu_rows(self, now: int) -> tuple[tuple[Button, ...], ...]:
        # The four primary destinations live on the persistent reply keyboard.
        # Journal has no persistent duplicate, so keep its existing callback.
        return ((self._btn(texts.MENU_JOURNAL, self.A_JOURNAL, now=now),),)

    def _explain(self, exc: DomainError) -> str:
        return str(exc)

    # ----------------------------------------------------------- entry events
    async def command_start(self, *, now: int) -> Reply:
        self._go(State.MENU, {}, now=now)
        return self._menu_reply(now)

    def unsupported(self, *, now: int) -> Reply:
        ui = self._ui()
        if ui.state in (State.CREATING, State.EDITING, State.AWAIT_REPLACE):
            return Reply(texts.UNSUPPORTED,
                         self._staged_rows(ui, now, staged=self._staged(ui)))
        return Reply(texts.UNSUPPORTED)

    def service_message(self) -> None:
        """Service messages are ignored silently: no reply, no state change."""
        return None

    # ---------------------------------------------------------- collection
    async def new_post(self, *, now: int) -> Reply:
        self._go(State.CREATING, {"staged": Staged().to_dict()}, now=now)
        return Reply(
            texts.ASK_CONTENT,
            (
                (self._btn(texts.BTN_CONTINUE, self.A_CONTINUE, now=now),),
                (self._btn(texts.BTN_CANCEL, self.A_BACK, now=now),),
            ),
        )

    async def text(self, *, now: int, text: str,
                   entities: tuple[Entity, ...] = (),
                   source_key: str | None = None) -> Reply:
        ui = self._ui()
        if source_key is not None and ui.state not in (
            State.CREATING, State.EDITING, State.AWAIT_REPLACE
        ) and self._state.has_effect(source_key):
            # a redelivered message must never reset or overwrite the newer
            # conversation step it lands in (PLAN §6 at-least-once)
            return self._replay_reply(now=now)
        if (ui.state is State.MANUAL_TIME and ui.arg('time_form')) or (
            ui.state is State.DRAFT and ui.arg('schedule_picker')
        ):
            try:
                if ui.state is State.DRAFT:
                    return self._text_schedule_picker(now=now, ui=ui, text=text,
                                                      source_key=source_key)
                return await self._text_time_form(now=now, ui=ui, text=text,
                                                  source_key=source_key)
            except EffectAlreadyApplied:
                return self._replay_reply(now=now)
        handler = {
            State.CREATING: self._text_creating,
            State.EDITING: self._text_editing,
            State.AWAIT_REPLACE: self._text_await_replace,
            State.MANUAL_TIME: self._text_manual_time,
            State.SETTINGS_INPUT: self._text_settings_input,
            State.RESOLUTION: self._text_resolution,
        }.get(ui.state)
        if handler is None:
            self._go(State.MENU, {}, now=now)
            return self._menu_reply(now)
        if ui.state in (State.CREATING, State.EDITING, State.AWAIT_REPLACE):
            # content intake: the effect is deduplicated per incoming message
            # so a redelivered update replays idempotently instead of
            # duplicating staged content or re-raising a false conflict
            try:
                return await handler(now=now, ui=ui, text=text,
                                     entities=entities, source_key=source_key)
            except EffectAlreadyApplied:
                return self._replay_reply(now=now)
            except ContentValidationError as exc:
                return self.content_rejected(now=now, exc=exc)
        reply = await handler(now=now, ui=ui, text=text, entities=entities)
        if reply.input_accepted and source_key is not None:
            self._state.record_effect(source_key, now_epoch=now)
        return reply

    async def media(self, *, now: int, kind: str, file_id: str,
                    caption: str | None,
                    caption_entities: tuple[Entity, ...] = (),
                    media_group_id: str | None = None,
                    source_key: str | None = None) -> Reply:
        ui = self._ui()
        if source_key is not None and ui.state not in (
            State.CREATING, State.EDITING, State.AWAIT_REPLACE
        ) and self._state.has_effect(source_key):
            return self._replay_reply(now=now)
        if ui.state not in (State.CREATING, State.EDITING, State.AWAIT_REPLACE):
            self._go(State.MENU, {}, now=now)
            return self._menu_reply(now)
        try:
            staged = self._staged(ui)
            result = staged.add_media(kind, file_id, caption, caption_entities,
                                      media_group_id)
        except ContentValidationError as exc:
            return self.content_rejected(now=now, exc=exc)
        try:
            if result == "ignored_in_album":
                # Not captured: leave the source visible with explicit feedback.
                return Reply(
                    "Элемент альбома другого типа (не фото/видео) пропущен: альбом "
                    "собирает только фото и видео."
                )
            if result == "conflict":
                return self._ask_replace(now=now, ui=ui, staged=staged,
                                         incoming=self._media_dict(
                                             kind, file_id, caption,
                                             caption_entities, media_group_id),
                                         source_key=source_key)
            self._state.set(ui.state, self._staged_payload(staged, ui),
                            expected_version=ui.version, now_epoch=now,
                            effect_key=source_key)
            return self._staged_reply(now=now, staged=staged)
        except EffectAlreadyApplied:
            return self._replay_reply(now=now)

    async def forwarded(self, *, now: int, src_chat_id: int, src_message_id: int,
                        media_group_id: str | None,
                        copy_candidate: Staged,
                        source_key: str | None = None) -> Reply:
        ui = self._ui()
        if source_key is not None and ui.state not in (
            State.CREATING, State.EDITING
        ) and self._state.has_effect(source_key):
            return self._replay_reply(now=now)
        if ui.state not in (State.CREATING, State.EDITING):
            self._go(State.MENU, {}, now=now)
            return self._menu_reply(now)
        try:
            staged = self._staged(ui)
            result = staged.add_forward(src_chat_id, src_message_id,
                                        media_group_id, copy_candidate)
        except ContentValidationError as exc:
            return self.content_rejected(now=now, exc=exc)
        try:
            if result == "conflict":
                return self._ask_replace(
                    now=now, ui=ui, staged=staged,
                    incoming=self._forward_dict(src_chat_id, src_message_id,
                                                media_group_id, copy_candidate),
                    source_key=source_key,
                )
            self._state.set(ui.state, self._staged_payload(staged, ui),
                            expected_version=ui.version, now_epoch=now,
                            effect_key=source_key)
            rows = [
                (self._btn(texts.BTN_FORWARD_AS_IS, self.A_FORWARD_AS_IS,
                           now=now),),
                (self._btn(texts.BTN_MAKE_COPY, self.A_FORWARD_COPY, now=now),),
            ]
            rows += list(self._staged_rows(ui, now, staged=staged))
            return Reply(texts.FORWARD_ASK, tuple(rows[:MAX_ROWS]))
        except EffectAlreadyApplied:
            return self._replay_reply(now=now)

    # ------------------------------------------------------- collection bits
    def _media_dict(self, kind, file_id, caption, caption_entities,
                    media_group_id) -> dict:
        return copy_candidate_from_media(
            kind, file_id, caption, caption_entities, media_group_id
        ).to_dict()

    def _forward_dict(self, chat_id, message_id, media_group_id,
                      copy_candidate) -> dict:
        staged = Staged()
        staged.add_forward(chat_id, message_id, media_group_id, copy_candidate)
        return staged.to_dict()

    def _ask_replace(self, *, now: int, ui: UiState, staged: Staged,
                     incoming: dict,
                     source_key: str | None = None) -> Reply:
        payload = self._staged_payload(staged, ui)
        payload["pending_replace"] = incoming
        payload["prev_state"] = str(ui.state)
        self._state.set(State.AWAIT_REPLACE, payload,
                        expected_version=ui.version, now_epoch=now,
                        effect_key=source_key)
        rows = (
            (self._btn(texts.BTN_REPLACE, self.A_REPLACE_YES, now=now),),
            (self._btn(texts.BTN_KEEP, self.A_REPLACE_KEEP, now=now),),
            (self._btn(texts.BTN_CANCEL, self.A_BACK, now=now),),
        )
        return Reply(texts.REPLACE_ASK, rows)

    def _caption_ask(self, *, now: int, ui: UiState, staged: Staged,
                     text: str, entities: tuple[Entity, ...],
                     source_key: str | None = None) -> Reply:
        payload = self._staged_payload(staged, ui)
        payload["pending_caption"] = {
            "text": text, "entities": entities_to_json(entities)
        }
        payload["prev_state"] = str(ui.state)
        self._state.set(State.AWAIT_REPLACE, payload,
                        expected_version=ui.version, now_epoch=now,
                        effect_key=source_key)
        rows = (
            (self._btn(texts.BTN_AS_CAPTION, self.A_CAPTION, now=now),),
            (self._btn(texts.BTN_REPLACE, self.A_REPLACE_YES, now=now),),
            (self._btn(texts.BTN_KEEP, self.A_REPLACE_KEEP, now=now),),
        )
        return Reply(texts.CAPTION_ASK, rows)

    # ------------------------------------------------- replay & rejection bits
    def _replay_reply(self, *, now: int) -> Reply:
        """A redelivered message whose durable effect already landed gets an
        idempotent answer rebuilt from the committed conversation — never a
        second effect, never a false conflict (PLAN §6 at-least-once)."""
        ui = self._ui()
        if ui.state is State.MANUAL_TIME and ui.arg('time_form'):
            return self._time_form_view(now=now)
        if ui.state is State.DRAFT and ui.arg('hours'):
            hours = ui.arg('hours')
            return self._hours_view(now=now, post_id=ui.arg('post_id'),
                                    day=date.fromisoformat(hours['day']),
                                    page=hours['page'], persist=False)
        if ui.state is State.AWAIT_REPLACE:
            pending_caption = ui.arg("pending_caption")
            if isinstance(pending_caption, dict):
                return Reply(texts.CAPTION_ASK, (
                    (self._btn(texts.BTN_AS_CAPTION, self.A_CAPTION, now=now),),
                    (self._btn(texts.BTN_REPLACE, self.A_REPLACE_YES, now=now),),
                    (self._btn(texts.BTN_KEEP, self.A_REPLACE_KEEP, now=now),),
                ))
            if isinstance(ui.arg("pending_replace"), dict):
                return Reply(texts.REPLACE_ASK, (
                    (self._btn(texts.BTN_REPLACE, self.A_REPLACE_YES, now=now),),
                    (self._btn(texts.BTN_KEEP, self.A_REPLACE_KEEP, now=now),),
                    (self._btn(texts.BTN_CANCEL, self.A_BACK, now=now),),
                ))
            return Reply(texts.REPLACE_ASK)
        if ui.state in (State.CREATING, State.EDITING):
            staged = self._staged(ui)
            if staged.kind == "forward":
                rows = [
                    (self._btn(texts.BTN_FORWARD_AS_IS, self.A_FORWARD_AS_IS,
                               now=now),),
                    (self._btn(texts.BTN_MAKE_COPY, self.A_FORWARD_COPY,
                               now=now),),
                ]
                rows += list(self._staged_rows(ui, now, staged=staged))
                return Reply(texts.FORWARD_ASK, tuple(rows[:MAX_ROWS]))
            return self._staged_reply(now=now, staged=staged)
        return self._menu_reply(now)

    def content_rejected(self, *, now: int,
                          exc: ContentValidationError) -> Reply:
        """Oversized/malformed incoming payloads are explained, never applied
        half-way: no state write happened before validation failed."""
        ui = self._ui()
        if ui.state in (State.CREATING, State.EDITING, State.AWAIT_REPLACE):
            return Reply(f"{texts.OVER_LIMITS_PREFIX}{exc}",
                         self._staged_rows(ui, now, staged=self._staged(ui)))
        return Reply(f"{texts.OVER_LIMITS_PREFIX}{exc}")

    def _staged_rows(self, ui: UiState, now: int, *, staged: Staged):
        rows = [
            (self._btn(texts.BTN_CONTINUE, self.A_CONTINUE, now=now),),
            (self._btn(texts.BTN_CANCEL, self.A_BACK, now=now),),
        ]
        return tuple(rows)

    def _staged_reply(self, *, now: int, staged: Staged) -> Reply:
        label, count = staged.summary()
        extra = ""
        if staged.kind == "album" and staged.ignored_group_items:
            extra = f" Пропущено неподдерживаемых элементов: {staged.ignored_group_items}."
        return Reply(
            f"В посте: {label} ({count}). {texts.BTN_CONTINUE} — когда всё "
            f"придёт.{extra}",
            self._staged_rows(self._ui(), now, staged=staged),
        )

    async def _text_creating(self, *, now: int, ui: UiState, text: str,
                             entities: tuple[Entity, ...],
                             source_key: str | None = None) -> Reply:
        staged = self._staged(ui)
        result = staged.add_text(text, entities)
        if result == "caption_possible":
            return self._caption_ask(now=now, ui=ui, staged=staged, text=text,
                                     entities=entities, source_key=source_key)
        if result == "conflict":
            incoming = copy_candidate_from_text(text, entities).to_dict()
            return self._ask_replace(now=now, ui=ui, staged=staged,
                                     incoming=incoming, source_key=source_key)
        self._state.set(ui.state, self._staged_payload(staged, ui),
                        expected_version=ui.version, now_epoch=now,
                        effect_key=source_key)
        return self._staged_reply(now=now, staged=staged)

    async def _text_editing(self, *, now: int, ui: UiState, text: str,
                            entities: tuple[Entity, ...],
                            source_key: str | None = None) -> Reply:
        return await self._text_creating(now=now, ui=ui, text=text,
                                         entities=entities,
                                         source_key=source_key)

    async def _text_await_replace(self, *, now: int, ui: UiState, text: str,
                                  entities: tuple[Entity, ...],
                                  source_key: str | None = None) -> Reply:
        # While waiting for an explicit choice, more content just updates the
        # pending candidate — the decision buttons are re-offered, the old
        # staged content is untouched.
        incoming = copy_candidate_from_text(text, entities).to_dict()
        payload = dict(ui.payload)
        payload["pending_replace"] = incoming
        self._state.set(ui.state, payload, expected_version=ui.version,
                        now_epoch=now, effect_key=source_key)
        rows = (
            (self._btn(texts.BTN_REPLACE, self.A_REPLACE_YES, now=now),),
            (self._btn(texts.BTN_KEEP, self.A_REPLACE_KEEP, now=now),),
            (self._btn(texts.BTN_CANCEL, self.A_BACK, now=now),),
        )
        return Reply(texts.REPLACE_ASK, rows)
    # ----------------------------------------------- continue / draft creation
    async def continue_pressed(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        if ui.state not in (State.CREATING, State.EDITING):
            self._go(State.MENU, {}, now=now)
            return Reply(texts.STEP_GONE, self._menu_reply(now).rows)
        staged = self._staged(ui)
        if staged.empty:
            return Reply(
                texts.NOTHING_STAGED, self._staged_rows(ui, now, staged=staged)
            )
        try:
            content = staged.build()
        except ContentValidationError as exc:
            return Reply(f"{texts.OVER_LIMITS_PREFIX}{exc}",
                         self._staged_rows(ui, now, staged=staged))
        if ui.state is State.EDITING:
            return self._commit_edit(now=now, ui=ui, content=content)
        post_id = self._drafts.create(content, now_epoch=now)
        self._journal.record("draft_created", post_id=post_id, now_epoch=now)
        outcome = await self._preview.send_preview(content)
        return self._draft_created_reply(now=now, post_id=post_id,
                                         outcome=outcome)

    def _commit_edit(self, *, now: int, ui: UiState, content: Content) -> Reply:
        target = ui.arg("target")
        try:
            if target == "draft":
                post_id = int(ui.arg("post_id"))
                self._drafts.update(post_id, content, now_epoch=now)
                self._journal.record("draft_updated", post_id=post_id,
                                     now_epoch=now)
                return self._draft_view(now=now, post_id=post_id,
                                        note=texts.CONTENT_UPDATED)
            pub_id = int(ui.arg("pub_id"))
            expected_version = int(ui.arg("expected_version"))
            self._publications.edit_content(pub_id, expected_version, content,
                                            now_epoch=now)
        except (DraftNotFound, DomainError) as exc:
            self._go(State.MENU, {}, now=now)
            return Reply(f"{texts.OP_FAILED_PREFIX}{exc}",
                         self._menu_reply(now).rows)
        return self._publication_view(now=now, pub_id=pub_id,
                                      note=texts.EDIT_PAUSED_NOTE)

    def _draft_created_reply(self, *, now: int, post_id: int,
                             outcome: PublishOutcome) -> Reply:
        note = texts.DRAFT_SAVED
        if isinstance(outcome, Sent):
            note = f"{note} {texts.PREVIEW_SENT}"
        else:
            code = getattr(outcome, "code", "api_error")
            reason = PREVIEW_FAIL_RU.get(code, code)
            note = f"{note} {texts.PREVIEW_FAILED_PREFIX}{reason}. {texts.PREVIEW_RETRY_HINT}"
        return self._draft_view(now=now, post_id=post_id, note=note)

    # ------------------------------------------------------------- replace/copy
    def replace_yes(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        incoming = ui.arg("pending_replace")
        if ui.state is not State.AWAIT_REPLACE or not isinstance(incoming, dict):
            self._go(State.MENU, {}, now=now)
            return Reply(texts.STEP_GONE, self._menu_reply(now).rows)
        prev = State(ui.arg("prev_state", State.CREATING.value))
        try:
            staged = Staged.from_dict(incoming)
        except ContentValidationError as exc:
            return Reply(f"{texts.OVER_LIMITS_PREFIX}{exc}",
                         self._staged_rows(ui, now, staged=self._staged(ui)))
        self._state.set(prev, {"staged": staged.to_dict()},
                        expected_version=ui.version, now_epoch=now)
        return self._staged_reply(now=now, staged=staged)

    def replace_keep(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        if ui.state is not State.AWAIT_REPLACE:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.STEP_GONE, self._menu_reply(now).rows)
        prev = State(ui.arg("prev_state", State.CREATING.value))
        staged = self._staged(ui)
        self._state.set(prev, {"staged": staged.to_dict()},
                        expected_version=ui.version, now_epoch=now)
        return self._staged_reply(now=now, staged=staged)

    def caption_yes(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        pending = ui.arg("pending_caption")
        if ui.state is not State.AWAIT_REPLACE or not isinstance(pending, dict):
            self._go(State.MENU, {}, now=now)
            return Reply(texts.STEP_GONE, self._menu_reply(now).rows)
        prev = State(ui.arg("prev_state", State.CREATING.value))
        staged = self._staged(ui)
        try:
            result = staged.set_caption(str(pending.get("text", "")),
                                        entities_from_json(
                                            pending.get("entities", [])))
        except ContentValidationError as exc:
            return Reply(f"{texts.OVER_LIMITS_PREFIX}{exc}",
                         self._staged_rows(ui, now, staged=self._staged(ui)))
        if result == "ambiguous":
            # distinct per-item captions exist: overwriting them with one
            # album caption is refused, the choice stays with the owner
            rows = (
                (self._btn(texts.BTN_REPLACE, self.A_REPLACE_YES, now=now),),
                (self._btn(texts.BTN_KEEP, self.A_REPLACE_KEEP, now=now),),
                (self._btn(texts.BTN_CANCEL, self.A_BACK, now=now),),
            )
            return Reply(CAPTION_AMBIGUOUS_RU, rows)
        self._state.set(prev, {"staged": staged.to_dict()},
                        expected_version=ui.version, now_epoch=now)
        return self._staged_reply(now=now, staged=staged)

    async def forward_as_is(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        staged = self._staged(ui)
        if ui.state not in (State.CREATING, State.EDITING) or staged.kind != "forward":
            self._go(State.MENU, {}, now=now)
            return Reply(texts.STEP_GONE, self._menu_reply(now).rows)
        try:
            content = staged.build()
        except ContentValidationError as exc:
            return Reply(f"{texts.OVER_LIMITS_PREFIX}{exc}",
                         self._staged_rows(ui, now, staged=staged))
        if ui.state is State.EDITING:
            return self._commit_edit(now=now, ui=ui, content=content)
        post_id = self._drafts.create(content, now_epoch=now)
        self._journal.record("draft_created", post_id=post_id, now_epoch=now)
        outcome = await self._preview.send_preview(content)
        return self._draft_created_reply(now=now, post_id=post_id,
                                         outcome=outcome)

    def forward_copy(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        staged = self._staged(ui)
        if ui.state not in (State.CREATING, State.EDITING) or staged.kind != "forward":
            self._go(State.MENU, {}, now=now)
            return Reply(texts.STEP_GONE, self._menu_reply(now).rows)
        copy = staged.copy
        try:
            # A nonempty branch is not proof of a usable or complete copy:
            # unsupported album items are omitted during candidate intake.
            # Validate the actual payload AND account for every source before
            # replacing durable coordinates or releasing any source to cleanup.
            if copy is None or copy.empty:
                raise ContentValidationError("no editable copy")
            content = copy.build()
            retained = len(content.items) if content.kind == "album" else 1
            if content.mode != "copy" or retained != len(staged.forward_ids):
                raise ContentValidationError("incomplete editable copy")
        except ContentValidationError:
            rows = (
                (self._btn(texts.BTN_FORWARD_AS_IS, self.A_FORWARD_AS_IS, now=now),),
                (self._btn(texts.BTN_MAKE_COPY, self.A_FORWARD_COPY, now=now),),
            ) + self._staged_rows(ui, now, staged=staged)
            return Reply(
                "Невозможно создать полную редактируемую копию этого содержимого. "
                "Источник сохранён: можно переслать как есть или нажать «Продолжить». "
                "Не удаляйте сообщения источника.", rows,
            )
        self._state.set(ui.state, self._staged_payload(copy, ui),
                        expected_version=ui.version, now_epoch=now)
        reply = self._staged_reply(now=now, staged=copy)
        # These coordinates come from durable intake, never callback data.
        # Validated file IDs/text now survive source deletion in explicit copy mode.
        released = tuple((int(staged.forward_chat_id), mid)
                         for mid in staged.forward_ids)
        return Reply(f"{texts.COPY_READY} {reply.text}", reply.rows,
                     released_inputs=released)

    # ------------------------------------------------------------------- drafts
    def drafts_view(self, *, now: int, page: int) -> Reply:
        ids = self._drafts.list_drafts()
        start, end, pages = page_window(len(ids), page)
        if not ids:
            self._go(State.MENU, {"view": "drafts"}, now=now)
            return Reply(texts.NO_DRAFTS, ((self._btn(texts.BTN_BACK, self.A_BACK, now=now),),))
        ui = self._go(State.MENU, {"view": "drafts"}, now=now)
        rows = []
        for pid in ids[start:end]:
            try:
                content = self._drafts.get_content(pid)
                line = self._content_line(content)
            except DraftNotFound:
                continue
            rows.append((self._btn(f"№{pid} · {line}", self.A_DRAFT_SELECT,
                                   now=now, post_id=pid, expected_version=ui.version,
                                   arg=self._draft_snapshot(content)),))
        nav = []
        if page > 0:
            nav.append(self._btn("← Пред.", self.A_DRAFTS, now=now,
                                 arg=f"p{page - 1}"))
        if page < pages - 1:
            nav.append(self._btn("След. →", self.A_DRAFTS, now=now,
                                 arg=f"p{page + 1}"))
        nav.append(self._btn(texts.BTN_BACK, self.A_BACK, now=now))
        rows.append(tuple(nav))
        return Reply(f"{texts.TITLE_DRAFTS} ({start + 1}–{end} из {len(ids)})",
                     tuple(rows[:MAX_ROWS]))

    def draft_open(self, *, now: int, post_id: int) -> Reply:
        return self._draft_view(now=now, post_id=post_id)

    @staticmethod
    def _draft_snapshot(content: Content) -> str:
        # Posts have no version field. Bind the server-side capability to the
        # immutable payload instead, without storing another copy of its text.
        return hashlib.sha256(content.to_json().encode()).hexdigest()

    def _draft_preview_valid(self, action: Action) -> bool:
        ui = self._ui()
        if action.expected_version != ui.version:
            return False
        if action.action == self.A_DRAFT_SELECT:
            if ui.state is not State.MENU or ui.arg("view") != "drafts":
                return False
        elif ui.state is not State.DRAFT or ui.arg("post_id") != action.post_id:
            return False
        if not self._drafts.is_draft(action.post_id):
            return False
        try:
            content = self._drafts.get_content(action.post_id)
        except DraftNotFound:
            return False
        return action.arg == self._draft_snapshot(content)

    async def draft_preview(self, *, now: int, post_id: int) -> Reply:
        try:
            content = self._drafts.get_content(post_id)
        except DraftNotFound:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.NOT_FOUND, self._menu_reply(now).rows)
        outcome = await self._preview.send_preview(content)
        if isinstance(outcome, Sent):
            note = texts.PREVIEW_SENT
        else:
            code = getattr(outcome, "code", "api_error")
            reason = PREVIEW_FAIL_RU.get(code, code)
            note = f"{texts.PREVIEW_FAILED_PREFIX}{reason}. {texts.PREVIEW_RETRY_HINT}"
        return self._draft_view(now=now, post_id=post_id, note=note)

    def draft_edit_start(self, *, now: int, post_id: int) -> Reply:
        try:
            self._drafts.get_content(post_id)
        except DraftNotFound:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.NOT_FOUND, self._menu_reply(now).rows)
        self._go(State.EDITING, {"target": "draft", "post_id": post_id,
                                 "staged": Staged().to_dict()}, now=now)
        return Reply(
            texts.EDIT_ASK,
            (
                (self._btn(texts.BTN_CONTINUE, self.A_CONTINUE, now=now),),
                (self._btn(texts.BTN_CANCEL, self.A_BACK, now=now),),
            ),
        )

    def draft_delete_ask(self, *, now: int, post_id: int) -> Reply:
        try:
            self._drafts.get_content(post_id)
        except DraftNotFound:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.NOT_FOUND, self._menu_reply(now).rows)
        rows = (
            (self._btn(texts.BTN_YES, self.A_DRAFT_DELETE_YES, now=now,
                       post_id=post_id),),
            (self._btn(texts.BTN_BACK, self.A_DRAFT_OPEN, now=now,
                       post_id=post_id),),
        )
        return Reply(f"{texts.DRAFT_DELETE_ASK} Отложенный пост №{post_id}.", rows)

    def draft_delete_yes(self, *, now: int, post_id: int) -> Reply:
        try:
            self._drafts.delete(post_id)
        except DraftNotFound as exc:
            self._go(State.MENU, {}, now=now)
            return Reply(f"{texts.OP_FAILED_PREFIX}{exc}",
                         self._menu_reply(now).rows)
        self._journal.record("draft_deleted", post_id=post_id, now_epoch=now)
        self._actions.invalidate(post_id=post_id)
        return self.drafts_view(now=now, page=0)

    # ------------------------------------------------------------- draft views
    def _content_line(self, content: Content) -> str:
        kind = texts.KIND_RU.get(content.kind, content.kind)
        if content.kind == "text":
            return f"{kind} ({len(content.text or '')} симв.)"
        if content.kind == "album":
            return f"{kind}, элементов: {len(content.items)}"
        if content.kind == "forward":
            fwd = content.forward
            return f"{kind}, сообщений: {len(fwd.message_ids) if fwd else 0}"
        return kind

    def _draft_view(self, *, now: int, post_id: int,
                    note: str | None = None) -> Reply:
        try:
            content = self._drafts.get_content(post_id)
        except DraftNotFound:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.NOT_FOUND, self._menu_reply(now).rows)
        self._go(State.DRAFT, {"post_id": post_id}, now=now)
        line = self._content_line(content)
        text = f"Отложенный пост №{post_id}. Содержимое: {line}."
        if note:
            text = f"{note}\n{text}"
        rows = (
            (self._btn(texts.BTN_SCHEDULE, self.A_DRAFT_SCHEDULE, now=now,
                       post_id=post_id),),
            (self._btn(texts.BTN_PREVIEW, self.A_DRAFT_PREVIEW, now=now,
                       post_id=post_id, expected_version=self._ui().version,
                       arg=self._draft_snapshot(content)),),
            (self._btn(texts.BTN_EDIT_CONTENT, self.A_DRAFT_EDIT, now=now,
                       post_id=post_id),),
            (self._btn(texts.BTN_DELETE, self.A_DRAFT_DELETE, now=now,
                       post_id=post_id),),
            (self._btn(texts.BTN_BACK, self.A_BACK, now=now),),
        )
        return Reply(text, rows)
    # ------------------------------------------------------------- scheduling
    def draft_schedule_ask(self, *, now: int, post_id: int) -> Reply:
        try:
            self._drafts.get_content(post_id)
        except DraftNotFound:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.NOT_FOUND, self._menu_reply(now).rows)
        if self._publications.get_by_post(post_id) is not None:
            pub = self._publications.get_by_post(post_id)
            return self._publication_view(now=now, pub_id=pub.id,
                                          note=texts.ALREADY_PUB_RU)
        return self._schedule_picker(now=now, post_id=post_id)

    def _schedule_picker(self, *, now: int, post_id: int) -> Reply:
        ui = self._ui()
        if not (ui.state is State.DRAFT and ui.arg('post_id') == post_id
                and ui.arg('schedule_picker')):
            self._go(State.DRAFT, {'post_id': post_id, 'schedule_picker': True},
                     now=now, expected_version=ui.version)
        settings = self._settings.get()
        rows = [
            (self._btn(texts.BTN_NEXT_SLOT, self.A_SCHED_NEXT, now=now,
                       post_id=post_id),),
            (self._btn(texts.BTN_OWN_TIME, self.A_SCHED_OWN, now=now,
                       post_id=post_id),),
        ]
        suggestions = recent_manual_suggestions(
            self._manual_times.recent(limit=3), now, settings.timezone, limit=2
        )
        for sug in suggestions:
            rows.append((self._btn(
                f"{sug.hh:02d}:{sug.mm:02d} → {format_local(sug.instant, settings.timezone)}",
                self.A_SCHED_RECENT, now=now, post_id=post_id,
                arg=f"{sug.hh}:{sug.mm}",
            ),))
        rows.append((self._btn(texts.BTN_BACK, self.A_BACK, now=now),))
        return Reply(texts.PICK_TIME, tuple(rows[:MAX_ROWS]))

    def sched_next(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        post_id = action.post_id
        if ui.state is not State.DRAFT or ui.arg("post_id") != post_id:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.STEP_GONE, self._menu_reply(now).rows)
        return self._hours_view(now=now, post_id=post_id)

    def sched_own(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        post_id = action.post_id
        if ui.state not in (State.DRAFT, State.CONFIRM_SCHEDULE) or ui.arg("post_id") != post_id:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.STEP_GONE, self._menu_reply(now).rows)
        return self._start_time_form(now=now, payload={"purpose": "schedule", "post_id": post_id})

    def sched_recent(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        post_id = action.post_id
        ok_state = (
            ui.state is State.DRAFT and ui.arg("post_id") == post_id
        ) or (
            ui.state is State.MANUAL_TIME and ui.arg("purpose") == "schedule"
            and ui.arg("post_id") == post_id
        )
        clock = parse_clock(action.arg or "")
        if not ok_state or clock is None or post_id is None:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.STEP_GONE, self._menu_reply(now).rows)
        settings = self._settings.get()
        suggestions = recent_manual_suggestions([clock], now, settings.timezone,
                                                limit=1)
        if not suggestions:
            return Reply(texts.TIME_NOT_FUTURE,
                         self._schedule_picker(now=now, post_id=post_id).rows)
        instant = suggestions[0].instant
        return self._confirm_schedule_view(now=now, post_id=post_id,
                                           instant=instant,
                                           manual=clock)

    def _time_ask_reply(self, *, now: int, settings,
                        back: Button | None = None) -> Reply:
        rows = []
        if back is not None:
            rows.append((back,))
        text = f"{texts.TIME_ASK}{settings.timezone})."
        return Reply(text, tuple(rows))

    def _confirm_schedule_view(self, *, now: int, post_id: int, instant: Instant,
                               manual: tuple[int, int] | None = None) -> Reply:
        try:
            line = self._content_line(self._drafts.get_content(post_id))
        except DraftNotFound:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.NOT_FOUND, self._menu_reply(now).rows)
        settings = self._settings.get()
        payload = {"post_id": post_id, "minute_epoch": instant.epoch}
        if manual is not None:
            payload["manual_hh"], payload["manual_mm"] = manual
        self._go(State.CONFIRM_SCHEDULE, payload, now=now)
        token = self._token(self.A_CONFIRM, now=now, post_id=post_id,
                            minute_epoch=instant.epoch,
                            expected_version=self._ui().version)
        rows = (
            (Button(texts.BTN_CONFIRM, token),),
            (self._btn(texts.BTN_OWN_TIME, self.A_SCHED_OWN, now=now,
                       post_id=post_id),),
            (self._btn(texts.BTN_CANCEL, self.A_BACK, now=now),),
        )
        text = (
            f"{texts.CONFIRM_SUMMARY_PREFIX}{line} — "
            f"{format_local(instant, settings.timezone)} "
            f"({settings.timezone}).\n{texts.CONFIRM_HINT}"
        )
        return Reply(text, rows)

    def confirm_schedule(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        minute = action.minute_epoch
        post_id = action.post_id
        if (
            ui.state is not State.CONFIRM_SCHEDULE
            or post_id is None or minute is None
            or ui.arg("post_id") != post_id
            or ui.arg("minute_epoch") != minute
        ):
            self._go(State.MENU, {}, now=now)
            return Reply(texts.BUTTON_STALE, self._menu_reply(now).rows)
        if minute <= now:
            return self._expired_time_confirmation(now=now, ui=ui, minute=minute)
        try:
            pub = self._publications.schedule(post_id, Instant(minute),
                                              now_epoch=now)
        except (MinuteTakenError, PastTimeError, ConflictError,
                DomainError) as exc:
            reason = self._schedule_refusal_ru(exc)
            if isinstance(exc, (MinuteTakenError, PastTimeError)):
                self._go(State.DRAFT, {"post_id": post_id}, now=now)
                return Reply(f"{reason}\n{texts.PICK_TIME}",
                             self._schedule_picker(now=now, post_id=post_id).rows)
            self._go(State.MENU, {}, now=now)
            return Reply(f"{texts.OP_FAILED_PREFIX}{reason}",
                         self._menu_reply(now).rows)
        hh, mm = ui.arg("manual_hh"), ui.arg("manual_mm")
        if isinstance(hh, int) and isinstance(mm, int):
            self._manual_times.record(hh, mm, now_epoch=now)
        return self._publication_view(
            now=now, pub_id=pub.id,
            note=f"{texts.SCHEDULED_PREFIX}{self._pub_time_local(pub)}",
        )

    def _schedule_refusal_ru(self, exc: DomainError) -> str:
        if isinstance(exc, MinuteTakenError):
            return texts.MINUTE_TAKEN_RU
        if isinstance(exc, PastTimeError):
            return texts.CONFIRM_BAD_TIME
        if isinstance(exc, ConflictError):
            return texts.ALREADY_PUB_RU
        return texts.CONFIRM_BAD_TIME

    def _expired_time_confirmation(self, *, now: int, ui: UiState,
                                   minute: int) -> Reply:
        # Keep the selected wall date/time editable, without calling a service
        # whose due-minute tolerance belongs to the sender, not this UI.
        local = Instant(minute).local(self._settings.get().timezone)
        payload = dict(ui.payload)
        payload.pop("minute_epoch", None)
        payload["purpose"] = (ui.arg("purpose") or "schedule").removesuffix("_confirm")
        payload["time_form"] = {"date": local.date().isoformat(),
                                "time": local.strftime("%H:%M"), "field": None}
        self._go(State.MANUAL_TIME, payload, now=now)
        return self._time_form_view(now=now, note=texts.TIME_NOT_FUTURE)

    # -------------------------------------------------- manual time text input
    async def _text_manual_time(self, *, now: int, ui: UiState, text: str,
                                entities: tuple[Entity, ...]) -> Reply:
        settings = self._settings.get()
        try:
            instant, hh, mm = resolve_manual_time(text, now, settings.timezone)
        except PastTimeError:
            return Reply(f"{texts.TIME_NOT_FUTURE}\n{texts.TIME_ASK}"
                         f"{settings.timezone}).", ())
        except (NonLocalTimeError, AmbiguousLocalTimeError):
            return Reply(f"{texts.TIME_TZ_ISSUE}\n{texts.TIME_ASK}"
                         f"{settings.timezone}).", ())
        except DomainError:
            return Reply(f"{texts.TIME_BAD_INPUT}\n{texts.TIME_ASK}"
                         f"{settings.timezone}).", ())
        purpose = ui.arg("purpose")
        if purpose == "schedule":
            return replace(self._confirm_schedule_view(now=now,
                                                post_id=int(ui.arg("post_id")),
                                                instant=instant, manual=(hh, mm)),
                           input_accepted=True)
        if purpose in ("edit_time", "replan"):
            return replace(self._confirm_minute_change_view(now=now, ui=ui,
                                                     instant=instant,
                                                     manual=(hh, mm)),
                           input_accepted=True)
        self._go(State.MENU, {}, now=now)
        return self._menu_reply(now)

    def _confirm_minute_change_view(self, *, now: int, ui: UiState,
                                    instant: Instant,
                                    manual: tuple[int, int]) -> Reply:
        purpose = ui.arg("purpose")
        pub_id = int(ui.arg("pub_id"))
        expected_version = int(ui.arg("expected_version"))
        settings = self._settings.get()
        kind = self.A_CONFIRM_TIME_EDIT if purpose == "edit_time" \
            else self.A_CONFIRM_REPLAN
        self._state.set(
            State.MANUAL_TIME,
            {"purpose": f"{purpose}_confirm", "pub_id": pub_id,
             "expected_version": expected_version, "minute_epoch": instant.epoch,
             "manual_hh": manual[0], "manual_mm": manual[1]},
            expected_version=ui.version, now_epoch=now,
        )
        token = self._token(kind, now=now, publication_id=pub_id,
                            expected_version=expected_version,
                            minute_epoch=instant.epoch, arg=f'ui:{self._ui().version}')
        rows = (
            (Button(texts.BTN_CONFIRM, token),),
            (self._btn(texts.BTN_CANCEL, self.A_BACK, now=now),),
        )
        text = (
            f"{texts.CONFIRM_SUMMARY_PREFIX}{format_local(instant, settings.timezone)} "
            f"({settings.timezone}).\n{texts.CONFIRM_HINT}"
        )
        return Reply(text, rows)

    def confirm_time_edit(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        pub_id, minute = action.publication_id, action.minute_epoch
        if (
            ui.state is not State.MANUAL_TIME
            or ui.arg("purpose") != "edit_time_confirm"
            or pub_id is None or minute is None
            or ui.arg("pub_id") != pub_id or ui.arg("minute_epoch") != minute
        ):
            self._go(State.MENU, {}, now=now)
            return Reply(texts.BUTTON_STALE, self._menu_reply(now).rows)
        if minute <= now:
            return self._expired_time_confirmation(now=now, ui=ui, minute=minute)
        try:
            pub = self._publications.edit_time(pub_id, action.expected_version,
                                               Instant(minute), now_epoch=now)
        except DomainError as exc:
            reason = self._schedule_refusal_ru(exc)
            note = (f"{reason} {texts.PICK_TIME}"
                    if isinstance(exc, (MinuteTakenError, PastTimeError))
                    else f"{texts.OP_FAILED_PREFIX}{reason}")
            return self._publication_view(now=now, pub_id=pub_id, note=note)
        self._record_manual(ui, now)
        return self._publication_view(
            now=now, pub_id=pub.id,
            note=f"{texts.TIME_EDITED} {texts.SCHEDULED_PREFIX}"
                 f"{self._pub_time_local(pub)}",
        )

    def confirm_replan(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        pub_id, minute = action.publication_id, action.minute_epoch
        if (
            ui.state is not State.MANUAL_TIME
            or ui.arg("purpose") != "replan_confirm"
            or pub_id is None or minute is None
            or ui.arg("pub_id") != pub_id or ui.arg("minute_epoch") != minute
        ):
            self._go(State.MENU, {}, now=now)
            return Reply(texts.BUTTON_STALE, self._menu_reply(now).rows)
        if minute <= now:
            return self._expired_time_confirmation(now=now, ui=ui, minute=minute)
        try:
            pub = self._publications.replan(pub_id, action.expected_version,
                                            Instant(minute), now_epoch=now)
        except DomainError as exc:
            reason = self._schedule_refusal_ru(exc)
            note = (f"{reason} {texts.PICK_TIME}"
                    if isinstance(exc, (MinuteTakenError, PastTimeError))
                    else f"{texts.OP_FAILED_PREFIX}{reason}")
            return self._publication_view(now=now, pub_id=pub_id, note=note)
        self._record_manual(ui, now)
        return self._publication_view(
            now=now, pub_id=pub.id,
            note=f"{texts.SCHEDULED_PREFIX}{self._pub_time_local(pub)}",
        )

    def _record_manual(self, ui: UiState, now: int) -> None:
        hh, mm = ui.arg("manual_hh"), ui.arg("manual_mm")
        if isinstance(hh, int) and isinstance(mm, int):
            self._manual_times.record(hh, mm, now_epoch=now)
    # ---------------------------------------------------------------- day plan
    def plan_today(self, *, now: int) -> Reply:
        settings = self._settings.get()
        return self._day_view(now=now, day=local_day(now, settings.timezone),
                              page=0)

    def plan_day(self, *, now: int, day_iso: str, page: int) -> Reply:
        try:
            day = date.fromisoformat(day_iso)
        except ValueError:
            return self.plan_today(now=now)
        return self._day_view(now=now, day=day, page=page)

    def _day_view(self, *, now: int, day: date, page: int,
                  note: str | None = None) -> Reply:
        settings = self._settings.get()
        start, end = day_bounds(day, settings.timezone)
        pubs = self._publications.scheduled_between(start, end)
        start_i, end_i, pages = page_window(len(pubs), page, size=DAY_PAGE_SIZE)
        ui = self._go(State.MENU, {"view": "day"}, now=now)
        rows = []
        for pub in pubs[start_i:end_i]:
            when = pub.scheduled_at.astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M")
            status = texts.status_marker(pub.status)
            excerpt = summary(self._dispatch.content_for_publication(pub.id))
            rows.append((self._btn(
                f"{when} | {status} | {excerpt}",
                self.A_PUB_OPEN, now=now, publication_id=pub.id,
                expected_version=pub.version, arg=f'row:{ui.version}',
            ),))
        rows.extend(self._day_nav_rows(now=now, day=day, page=page,
                                       pages=pages))
        head = f"{texts.PLAN_DAY_PREFIX}{day.strftime('%d.%m.%Y')} " \
               f"({settings.timezone})"
        if not pubs:
            body = texts.NO_PLANS_DAY
        else:
            body = f"{start_i + 1}–{end_i} из {len(pubs)}"
        text = f"{head}: {body}."
        if note:
            text = f"{note}\n{text}"
        return Reply(text, tuple(rows[:MAX_ROWS]))

    def _day_nav_rows(self, *, now: int, day: date, page: int, pages: int):
        today_day = local_day(now, self._settings.get().timezone)
        day_iso = day.isoformat()
        back_row: list[Button] = []
        if page > 0:
            back_row.append(self._btn("↑ Сверху", self.A_DAY, now=now,
                                      arg=f"{day_iso}:0"))
        if page < pages - 1:
            back_row.append(self._btn(f"Ещё ↓ {page + 2}/{pages}", self.A_DAY,
                                      now=now, arg=f"{day_iso}:{page + 1}"))
        back_row.append(self._btn(texts.BTN_BACK, self.A_BACK, now=now))
        nav = (
            self._btn("← Пред.", self.A_DAY, now=now,
                      arg=f"{(day - timedelta(days=1)).isoformat()}:0"),
            self._btn("Сегодня", self.A_DAY, now=now,
                      arg=f"{today_day.isoformat()}:0"),
            self._btn("След. →", self.A_DAY, now=now,
                      arg=f"{(day + timedelta(days=1)).isoformat()}:0"),
        )
        return (nav, tuple(back_row))

    def _last_permalink(self, post_id: int) -> str | None:
        links = self._dispatch.history_links(post_id)
        if not links:
            return None
        return links[-1]["permalink"] or None

    # ------------------------------------------------------ publication views
    def _publication_view(self, *, now: int, pub_id: int,
                          note: str | None = None) -> Reply:
        try:
            pub = self._publications.get(pub_id)
        except DomainError:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.NOT_FOUND, self._menu_reply(now).rows)
        self._go(State.PUBLICATION, {"pub_id": pub_id}, now=now)
        try:
            line = self._content_line(
                self._dispatch.content_for_publication(pub_id))
        except DomainError:
            line = "—"
        head = (
            f"{texts.PUB_TITLE} №{pub.id}. Статус: {texts.STATUS_RU[pub.status]}.\n"
            f"Время: {self._pub_time_local(pub)}. Содержимое: {line}. "
            f"Пост: №{pub.post_id}."
        )
        text = f"{note}\n{head}" if note else head
        rows = self._pub_rows(now=now, pub=pub)
        return Reply(text, rows)

    def _pub_rows(self, *, now: int, pub) -> tuple[tuple[Button, ...], ...]:
        rows: list[tuple[Button, ...]] = []
        add = rows.append
        st = pub.status
        if st in (Status.SCHEDULED, Status.SUSPENDED, Status.MISSED, Status.UNCERTAIN,
                  Status.PUBLISHED):
            add((self._btn(texts.BTN_PREVIEW, self.A_PUB_PREVIEW, now=now,
                           publication_id=pub.id, expected_version=pub.version),))
        if st is Status.SCHEDULED:
            add((self._btn(texts.BTN_PAUSE, self.A_PUB_PAUSE, now=now,
                           publication_id=pub.id, expected_version=pub.version),))
            add((self._btn(texts.BTN_EDIT_CONTENT, self.A_PUB_EDIT_CONTENT,
                           now=now, publication_id=pub.id),))
            add((self._btn(texts.BTN_EDIT_TIME, self.A_PUB_EDIT_TIME, now=now,
                           publication_id=pub.id, expected_version=pub.version),))
        if st is Status.SUSPENDED:
            add((self._btn(texts.BTN_RESUME, self.A_PUB_RESUME, now=now,
                           publication_id=pub.id, expected_version=pub.version),))
            add((self._btn(texts.BTN_EDIT_TIME, self.A_PUB_EDIT_TIME, now=now,
                           publication_id=pub.id, expected_version=pub.version),))
        if st is Status.MISSED:
            add((self._btn(texts.BTN_REPLAN, self.A_PUB_REPLAN, now=now,
                           publication_id=pub.id, expected_version=pub.version),))
        if st in (Status.SCHEDULED, Status.SUSPENDED, Status.MISSED):
            add((self._btn(texts.BTN_TO_DRAFTS, self.A_PUB_CANCEL, now=now,
                           publication_id=pub.id, expected_version=pub.version),))
            add((self._btn(texts.BTN_DELETE, self.A_PUB_DELETE, now=now,
                           publication_id=pub.id, post_id=pub.post_id),))
        if st is Status.PUBLISHED:
            link = self._last_permalink(pub.post_id)
            if link:
                add((Button(texts.BTN_LINK, url=link),))
        if st is Status.UNCERTAIN:
            add((self._btn(texts.BTN_MARK_PUBLISHED, self.A_MARK_PUBLISHED,
                           now=now, publication_id=pub.id,
                           expected_version=pub.version),))
            add((self._btn(texts.BTN_TO_DRAFTS, self.A_TO_DRAFT_RISK, now=now,
                           publication_id=pub.id,
                           expected_version=pub.version),))
        add((self._btn(texts.BTN_BACK, self.A_BACK, now=now),))
        return tuple(rows[:MAX_ROWS])

    def _pub_time_local(self, pub) -> str:
        settings = self._settings.get()
        return format_local(Instant(pub.scheduled_epoch), settings.timezone)

    # ------------------------------------------------- publication management
    def pub_open(self, *, now: int, pub_id: int) -> Reply:
        return self._publication_view(now=now, pub_id=pub_id)

    async def pub_preview(self, *, now: int, pub_id: int) -> Reply:
        # Read the actual payload bound to this publication.
        # No claim, scheduling mutation or source cleanup belongs in this path.
        try:
            content = self._dispatch.content_for_publication(pub_id)
        except DomainError:
            return Reply(texts.BUTTON_STALE, (), stale_callback=True)
        outcome = await self._preview.send_preview(content)
        if isinstance(outcome, Sent):
            note = texts.PREVIEW_SENT
        else:
            code = getattr(outcome, "code", "api_error")
            reason = PREVIEW_FAIL_RU.get(code, code)
            note = f"{texts.PREVIEW_FAILED_PREFIX}{reason}. {texts.PUB_PREVIEW_RETRY_HINT}"
        return self._publication_view(now=now, pub_id=pub_id, note=note)

    def pub_pause(self, *, now: int, action: Action) -> Reply:
        pub_id = int(action.publication_id)
        try:
            self._publications.pause(pub_id, int(action.expected_version),
                                     now_epoch=now)
        except DomainError as exc:
            return self._publication_view(
                now=now, pub_id=pub_id,
                note=f"{texts.OP_FAILED_PREFIX}{self._pub_refusal_ru(exc)}")
        return self._publication_view(now=now, pub_id=pub_id)

    def pub_edit_content(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        pub_id = int(action.publication_id)
        if ui.state is not State.PUBLICATION or ui.arg("pub_id") != pub_id:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.BUTTON_STALE, self._menu_reply(now).rows)
        try:
            pub = self._publications.get(pub_id)
        except DomainError:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.NOT_FOUND, self._menu_reply(now).rows)
        self._go(State.EDITING, {"target": "pub", "pub_id": pub_id,
                                 "expected_version": pub.version,
                                 "staged": Staged().to_dict()}, now=now)
        return Reply(
            texts.EDIT_ASK,
            (
                (self._btn(texts.BTN_CONTINUE, self.A_CONTINUE, now=now),),
                (self._btn(texts.BTN_CANCEL, self.A_BACK, now=now),),
            ),
        )

    def pub_edit_time(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        pub_id = int(action.publication_id)
        if ui.state is not State.PUBLICATION or ui.arg("pub_id") != pub_id:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.BUTTON_STALE, self._menu_reply(now).rows)
        return self._start_time_form(now=now, payload={
            "purpose": "edit_time", "pub_id": pub_id,
            "expected_version": int(action.expected_version)})

    def pub_resume(self, *, now: int, action: Action) -> Reply:
        pub_id = int(action.publication_id)
        try:
            self._publications.resume(pub_id, int(action.expected_version),
                                      now_epoch=now)
        except PastTimeError:
            return self._publication_view(now=now, pub_id=pub_id,
                                          note=texts.PUB_RESUME_PAST)
        except DomainError as exc:
            return self._publication_view(
                now=now, pub_id=pub_id,
                note=f"{texts.OP_FAILED_PREFIX}{self._pub_refusal_ru(exc)}")
        return self._publication_view(now=now, pub_id=pub_id)

    def pub_cancel_ask(self, *, now: int, action: Action) -> Reply:
        pub_id = int(action.publication_id)
        rows = (
            (self._btn(texts.BTN_YES, self.A_PUB_CANCEL_YES, now=now,
                       publication_id=pub_id,
                       expected_version=action.expected_version),),
            (self._btn(texts.BTN_BACK, self.A_PUB_OPEN, now=now,
                       publication_id=pub_id),),
        )
        return Reply(f"{texts.PUB_CANCEL_ASK} №{pub_id}.", rows)

    def pub_cancel_yes(self, *, now: int, action: Action) -> Reply:
        pub_id = int(action.publication_id)
        try:
            pub = self._publications.get(pub_id)
            self._publications.cancel_to_draft(pub_id,
                                               int(action.expected_version),
                                               now_epoch=now)
        except DomainError as exc:
            return self._publication_view(
                now=now, pub_id=pub_id,
                note=f"{texts.OP_FAILED_PREFIX}{self._pub_refusal_ru(exc)}")
        self._actions.invalidate(publication_id=pub_id)
        return self._draft_view(now=now, post_id=pub.post_id,
                                note=texts.TO_DRAFTS_DONE)

    def pub_delete_ask(self, *, now: int, action: Action) -> Reply:
        rows = (
            (self._btn(texts.BTN_YES, self.A_PUB_DELETE_YES, now=now,
                       post_id=action.post_id),),
            (self._btn(texts.BTN_BACK, self.A_PUB_OPEN, now=now,
                       publication_id=action.publication_id),),
        )
        return Reply(f"{texts.PUB_DELETE_ASK} Пост №{action.post_id}.", rows)

    def pub_delete_yes(self, *, now: int, action: Action) -> Reply:
        post_id = int(action.post_id)
        try:
            self._publications.delete_by_post(post_id, now_epoch=now)
        except DomainError as exc:
            self._go(State.MENU, {}, now=now)
            return Reply(f"{texts.OP_FAILED_PREFIX}{self._pub_refusal_ru(exc)}",
                         self._menu_reply(now).rows)
        self._actions.invalidate(post_id=post_id)
        self._go(State.MENU, {}, now=now)
        return Reply(texts.DELETED, self._menu_reply(now).rows)

    def pub_replan(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        pub_id = int(action.publication_id)
        if ui.state is not State.PUBLICATION or ui.arg("pub_id") != pub_id:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.BUTTON_STALE, self._menu_reply(now).rows)
        return self._start_time_form(now=now, payload={
            "purpose": "replan", "pub_id": pub_id,
            "expected_version": int(action.expected_version)})

    def _pub_refusal_ru(self, exc: DomainError) -> str:
        if isinstance(exc, TransitionError):
            if "immutable" in str(exc) or "in flight" in str(exc):
                return texts.IMMUTABLE_SENDING
            if "replan" in str(exc) or "re-confirmed" in str(exc):
                return texts.PUB_RESUME_PAST
        return self._schedule_refusal_ru(exc)

    # ------------------------------------------------------ uncertain handling
    def mark_published_ask(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        pub_id = int(action.publication_id)
        if ui.state is not State.PUBLICATION or ui.arg("pub_id") != pub_id:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.BUTTON_STALE, self._menu_reply(now).rows)
        try:
            pub = self._publications.get(pub_id)
        except DomainError:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.NOT_FOUND, self._menu_reply(now).rows)
        self._go(State.RESOLUTION, {"pub_id": pub_id,
                                    "expected_version": pub.version}, now=now)
        return Reply(
            texts.RESOLUTION_ASK,
            ((self._btn(texts.BTN_BACK, self.A_PUB_OPEN, now=now,
                        publication_id=pub_id),),),
        )

    async def _text_resolution(self, *, now: int, ui: UiState, text: str,
                               entities: tuple[Entity, ...]) -> Reply:
        pub_id = int(ui.arg("pub_id"))
        expected_version = int(ui.arg("expected_version"))
        message_id = self._parse_channel_ref(text)
        if message_id is None:
            return Reply(
                f"{texts.RESOLUTION_BAD}\n{texts.RESOLUTION_ASK}",
                ((self._btn(texts.BTN_BACK, self.A_PUB_OPEN, now=now,
                            publication_id=pub_id),),),
            )
        permalink = self._channel_permalink(message_id)
        try:
            self._dispatch.resolve_uncertain_as_published(
                pub_id, expected_version, (message_id,), permalink,
                now_epoch=now)
        except DomainError as exc:
            return self._publication_view(
                now=now, pub_id=pub_id,
                note=f"{texts.OP_FAILED_PREFIX}{self._pub_refusal_ru(exc)}")
        return replace(self._publication_view(now=now, pub_id=pub_id,
                                      note=texts.MARKED_PUBLISHED),
                       input_accepted=True)

    def _parse_channel_ref(self, text: str) -> int | None:
        raw = text.strip()
        if raw.isdigit():
            value = int(raw)
            return value if value > 0 else None
        match = re.search(r"/(\d+)/?(?:\?.*)?$", raw)
        if match and raw.startswith(("http://", "https://")):
            return int(match.group(1))
        return None

    def _channel_permalink(self, message_id: int) -> str:
        # public channel: the configured username produces the canonical
        # t.me/<username>/<id> link; numeric targets fall back to t.me/c
        if self._channel_username:
            return f"https://t.me/{self._channel_username}/{message_id}"
        raw = str(abs(int(self._channel_id)))
        if raw.startswith("100"):
            raw = raw[3:]
        return f"https://t.me/c/{raw}/{message_id}"

    def to_draft_risk_ask(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        pub_id = int(action.publication_id)
        if ui.state is not State.PUBLICATION or ui.arg("pub_id") != pub_id:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.BUTTON_STALE, self._menu_reply(now).rows)
        self._go(State.ACK_RISK, {"pub_id": pub_id,
                                  "expected_version": action.expected_version},
                 now=now)
        rows = (
            (self._btn(texts.BTN_ACK_RISK, self.A_ACK_RISK, now=now,
                       publication_id=pub_id,
                       expected_version=action.expected_version),),
            (self._btn(texts.BTN_BACK, self.A_PUB_OPEN, now=now,
                       publication_id=pub_id),),
        )
        return Reply(texts.ACK_RISK_TEXT, rows)

    def ack_risk(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        pub_id = int(action.publication_id)
        if (
            ui.state is not State.ACK_RISK
            or ui.arg("pub_id") != pub_id
            or ui.arg("expected_version") != action.expected_version
        ):
            self._go(State.MENU, {}, now=now)
            return Reply(f"{texts.ACK_REQUIRED} {texts.BUTTON_STALE}",
                         self._menu_reply(now).rows)
        post_row = self._conn.execute(
            "SELECT post_id FROM publications WHERE id=?", (pub_id,)
        ).fetchone()
        try:
            self._dispatch.resolve_uncertain_to_draft(
                pub_id, int(action.expected_version), acknowledged=True,
                now_epoch=now)
        except DomainError as exc:
            return self._publication_view(
                now=now, pub_id=pub_id,
                note=f"{texts.OP_FAILED_PREFIX}{self._pub_refusal_ru(exc)}")
        self._actions.invalidate(publication_id=pub_id)
        if post_row is not None:
            return self._draft_view(now=now, post_id=int(post_row["post_id"]),
                                    note=texts.TO_DRAFTS_DONE)
        self._go(State.MENU, {}, now=now)
        menu = self._menu_reply(now)
        return Reply(f"{texts.TO_DRAFTS_DONE}\n{menu.text}", menu.rows)
    # ---------------------------------------------------------------- settings
    def settings_view(self, *, now: int) -> Reply:
        current = self._settings.get()
        self._go(State.SETTINGS, {}, now=now)
        text = (
            f"{texts.TITLE_SETTINGS}:\n"
            f"{texts.SET_TZ_LABEL}: {current.timezone}\n"
            f"{texts.SET_START_LABEL}: {current.start_hour}\n"
            f"{texts.SET_END_LABEL}: {current.end_hour}\n"
            f"{texts.SETTINGS_NOTE}"
        )
        rows = (
            (self._btn(texts.SET_TZ_LABEL, self.A_SET_TZ, now=now,
                       arg=KEY_TZ),),
            (self._btn(texts.SET_START_LABEL, self.A_SET_START, now=now,
                       arg=KEY_START),),
            (self._btn(texts.SET_END_LABEL, self.A_SET_END, now=now,
                       arg=KEY_END),),
            (self._btn(texts.BTN_BACK, self.A_BACK, now=now),),
        )
        return Reply(text, rows)

    def settings_edit(self, *, now: int, action: Action) -> Reply:
        key = action.arg
        if key == KEY_TZ:
            return self.timezone_view(now=now)
        if key not in (KEY_TZ, KEY_START, KEY_END):
            self._go(State.MENU, {}, now=now)
            return Reply(texts.STEP_GONE, self._menu_reply(now).rows)
        current = self._settings.get()
        label, value = {
            KEY_TZ: (texts.SET_TZ_LABEL, current.timezone),
            KEY_START: (texts.SET_START_LABEL, str(current.start_hour)),
            KEY_END: (texts.SET_END_LABEL, str(current.end_hour)),
        }[key]
        self._go(State.SETTINGS_INPUT, {"key": key}, now=now)
        return Reply(
            f"{label}: {value}. {texts.SETTINGS_VALUE_ASK}",
            ((self._btn(texts.BTN_BACK, self.A_SETTINGS, now=now),),),
        )

    async def _text_settings_input(self, *, now: int, ui: UiState, text: str,
                                   entities: tuple[Entity, ...]) -> Reply:
        key = ui.arg("key")
        if key == KEY_TZ and ui.arg("tz_picker"):
            return replace(self.timezone_view(now=now, query=text.strip()),
                           input_accepted=True)
        if key not in (KEY_TZ, KEY_START, KEY_END):
            self._go(State.MENU, {}, now=now)
            return self._menu_reply(now)
        try:
            self._settings.change(**{key: text.strip()})
        except SettingsError:
            bad = (texts.SETTINGS_BAD_TZ if key == KEY_TZ
                   else texts.SETTINGS_BAD_HOUR)
            return Reply(
                f"{bad}\n{texts.SETTINGS_VALUE_ASK}",
                ((self._btn(texts.BTN_BACK, self.A_SETTINGS, now=now),),),
            )
        self._journal.record("settings_changed", keys_changed=key,
                             now_epoch=now)
        return replace(self.settings_view(now=now), input_accepted=True)

    def timezone_view(self, *, now: int, page: int | None = None,
                      all_zones: bool = False, query: str = "") -> Reply:
        current = self._settings.get().timezone
        zones = timezones.choices(all_zones=all_zones, query=query)
        if not query and current not in zones:
            zones.append(current)
        if page is None:
            page = zones.index(current) // timezones.PAGE_SIZE if current in zones else 0
        pages = max(1, (len(zones) + timezones.PAGE_SIZE - 1) // timezones.PAGE_SIZE)
        page = min(max(0, page), pages - 1)
        ui = self._go(State.SETTINGS_INPUT,
                      {"key": KEY_TZ, "tz_picker": True, "tz_query": query,
                       "tz_all": all_zones}, now=now)
        rows = [(self._btn(timezones.label(zone, current=current, now=now),
                           "tz_choose", now=now, arg=zone,
                           expected_version=ui.version),)
                for zone in zones[page * timezones.PAGE_SIZE:
                                  (page + 1) * timezones.PAGE_SIZE]]
        nav = []
        for target, title in ((page - 1, "← Пред."), (page + 1, "След. →")):
            if 0 <= target < pages:
                nav.append(self._btn(title, "tz_page", now=now, arg=str(target),
                                     expected_version=ui.version))
        if nav:
            rows.append(tuple(nav))
        rows.append((self._btn("Все пояса IANA", "tz_all", now=now,
                               expected_version=ui.version),
                     self._btn("Ввести IANA вручную", "tz_manual", now=now,
                               expected_version=ui.version)))
        rows.append((self._btn(texts.BTN_BACK, self.A_SETTINGS, now=now),))
        return Reply(
            f"Часовой пояс · {page + 1}/{pages}\n"
            f"Текущий: {current} ({timezones.label(current, current=current, now=now)})\n"
            "Выберите город или пришлите название города / часть IANA для поиска."
            + ("\nНичего не найдено." if not zones else ""), tuple(rows))

    # ---------------------------------------------------------------- journal
    def journal_view(self, *, now: int, page: int) -> Reply:
        total = self._journal.total()
        start, end, pages = page_window(total, page)
        lines = [self._journal_line(row)
                 for row in self._journal.entries_page(start, PAGE_SIZE)]
        if lines:
            text = f"{texts.TITLE_JOURNAL} ({start + 1}–{end} из {total}):\n" \
                   + "\n".join(lines)
        else:
            text = f"{texts.TITLE_JOURNAL}: {texts.NO_JOURNAL}"
        nav = []
        if page > 0:
            nav.append(self._btn("← Пред.", self.A_JOURNAL, now=now,
                                 arg=f"p{page - 1}"))
        if page < pages - 1:
            nav.append(self._btn("След. →", self.A_JOURNAL, now=now,
                                 arg=f"p{page + 1}"))
        nav.append(self._btn(texts.BTN_BACK, self.A_BACK, now=now))
        self._go(State.MENU, {"view": "journal"}, now=now)
        return Reply(text, (tuple(nav),))

    def _journal_line(self, row) -> str:
        parts = [row["ts"], texts.JOURNAL_RU.get(row["kind"], row["kind"])]
        if row["post_id"] is not None:
            parts.append(f"post №{row['post_id']}")
        if row["publication_id"] is not None:
            parts.append(f"pub №{row['publication_id']}")
        try:
            detail = json.loads(row["detail"] or "{}")
        except json.JSONDecodeError:
            detail = {}
        if isinstance(detail, dict):
            for name in ("outcome", "code", "reason_code", "keys_changed"):
                value = detail.get(name)
                if isinstance(value, str):
                    parts.append(CODE_RU.get(value, value))
            for name in ("minute_epoch",):
                value = detail.get(name)
                if isinstance(value, int):
                    parts.append(f"минута {value}")
            for name in ("message_count", "removed_count", "affected_count",
                         "downtime_seconds"):
                value = detail.get(name)
                if isinstance(value, int):
                    parts.append(f"{name}={value}")
        return " · ".join(parts)

    # ------------------------------------------------------------ press routing
    async def press(self, *, now: int, token: str,
                    on_consumed: Callable[[], Awaitable[None]] | None = None,
                    refresh_now: Callable[[], int] | None = None) -> Reply:
        try:
            action = self._actions.consume(token, now_epoch=now)
        except ActionStale:
            return Reply(texts.BUTTON_STALE, (), stale_callback=True)
        ui = self._ui()
        if action.action == self.A_PUB_OPEN and action.expected_version is not None:
            if (action.arg != f'row:{ui.version}' or ui.state is not State.MENU
                    or ui.arg('view') not in (None, 'day')):
                return Reply(texts.BUTTON_STALE, (), stale_callback=True)
            try:
                pub = self._publications.get(action.publication_id)
            except DomainError:
                return Reply(texts.BUTTON_STALE, (), stale_callback=True)
            if pub.version != action.expected_version:
                return Reply(texts.BUTTON_STALE, (), stale_callback=True)
        if (ui.arg('time_form') or ui.arg('hours')) and action.action not in CARD_ACTIONS:
            # Inline buttons from an older screen cannot replace a live card.
            # The persistent reply keyboard still navigates via menu_navigation.
            return Reply(texts.BUTTON_STALE, (), stale_callback=True)
        if action.action in CARD_ACTIONS | {self.A_SCHED_NEXT, self.A_SCHED_OWN,
                                           self.A_SCHED_RECENT, self.A_CONFIRM}:
            if action.expected_version != self._ui().version:
                return Reply(texts.BUTTON_STALE, (), stale_callback=True)
        if action.action in (self.A_CONFIRM_TIME_EDIT, self.A_CONFIRM_REPLAN):
            if action.arg != f'ui:{self._ui().version}':
                return Reply(texts.BUTTON_STALE, (), stale_callback=True)
        if action.action in ("tz_choose", "tz_page", "tz_all", "tz_manual"):
            ui = self._ui()
            if (ui.state != State.SETTINGS_INPUT or not ui.arg("tz_picker")
                    or action.expected_version != ui.version):
                return Reply(texts.BUTTON_STALE, (), stale_callback=True)
        if action.action in (self.A_DRAFT_SELECT, self.A_DRAFT_PREVIEW):
            if not self._draft_preview_valid(action):
                return Reply(texts.BUTTON_STALE, (), stale_callback=True)
        if action.action == self.A_PUB_PREVIEW:
            if (ui.state is not State.PUBLICATION
                    or ui.arg("pub_id") != action.publication_id):
                return Reply(texts.BUTTON_STALE, (), stale_callback=True)
            try:
                pub = self._publications.get(action.publication_id)
            except DomainError:
                return Reply(texts.BUTTON_STALE, (), stale_callback=True)
            if (pub.version != action.expected_version or pub.status not in (
                    Status.SCHEDULED, Status.SUSPENDED, Status.MISSED, Status.UNCERTAIN,
                    Status.PUBLISHED)):
                return Reply(texts.BUTTON_STALE, (), stale_callback=True)
        # Acknowledge validated callbacks before potentially slow previews.
        # Stale tokens return above and get one explanatory toast instead.
        if on_consumed is not None:
            await on_consumed()
        # Acknowledgement is network I/O and can cross the selected minute.
        # Direct/offline callers retain their explicit deterministic `now`.
        if refresh_now is not None:
            now = refresh_now()
        return await self._run_action(now=now, action=action)

    async def _run_action(self, *, now: int, action: Action) -> Reply:
        kind = action.action
        if kind in (self.A_DRAFT_SELECT, self.A_DRAFT_PREVIEW):
            if not self._draft_preview_valid(action):
                return Reply(texts.BUTTON_STALE, (), stale_callback=True)
            return await self.draft_preview(now=now, post_id=action.post_id)
        if kind in (self.A_PUB_OPEN, self.A_PUB_PREVIEW) and action.expected_version is not None:
            # The acknowledgement itself awaited network I/O. Recheck the
            # capability before sending content if the publication changed.
            try:
                current = self._publications.get(action.publication_id)
            except DomainError:
                return Reply(texts.BUTTON_STALE, (), stale_callback=True)
            if current.version != action.expected_version:
                return Reply(texts.BUTTON_STALE, (), stale_callback=True)
        if kind in CARD_ACTIONS:
            return self._card_action(now=now, action=action)
        if kind in ("tz_choose", "tz_page", "tz_all", "tz_manual"):
            ui = self._ui()
            if kind == "tz_choose":
                try:
                    self._settings.change(**{KEY_TZ: action.arg})
                except SettingsError:
                    return Reply(texts.SETTINGS_BAD_TZ)
                self._journal.record("settings_changed", keys_changed=KEY_TZ,
                                     now_epoch=now)
                return self.settings_view(now=now)
            if kind == "tz_manual":
                self._go(State.SETTINGS_INPUT, {"key": KEY_TZ}, now=now)
                return Reply(texts.SETTINGS_VALUE_ASK + " Пример: Europe/Moscow.",
                             ((self._btn(texts.BTN_BACK, self.A_SET_TZ,
                                         now=now, arg=KEY_TZ),),))
            return self.timezone_view(
                now=now, page=int(action.arg or 0) if kind == "tz_page" else 0,
                all_zones=kind == "tz_all" or ui.arg("tz_all", False),
                query=ui.arg("tz_query", "") if kind == "tz_page" else "")
        if kind == self.A_MENU:
            return await self.command_start(now=now)
        if kind == self.A_NEW:
            return await self.new_post(now=now)
        if kind == self.A_CONTINUE:
            return await self.continue_pressed(now=now, action=action)
        if kind == self.A_REPLACE_YES:
            return self.replace_yes(now=now, action=action)
        if kind == self.A_REPLACE_KEEP:
            return self.replace_keep(now=now, action=action)
        if kind == self.A_CAPTION:
            return self.caption_yes(now=now, action=action)
        if kind == self.A_FORWARD_AS_IS:
            return await self.forward_as_is(now=now, action=action)
        if kind == self.A_FORWARD_COPY:
            return self.forward_copy(now=now, action=action)
        if kind == self.A_DRAFTS:
            return self.drafts_view(now=now, page=self._page_of(action.arg))
        if kind == self.A_DRAFT_OPEN and action.post_id is not None:
            return self.draft_open(now=now, post_id=action.post_id)
        if kind == self.A_DRAFT_PREVIEW and action.post_id is not None:
            return await self.draft_preview(now=now, post_id=action.post_id)
        if kind == self.A_DRAFT_EDIT and action.post_id is not None:
            return self.draft_edit_start(now=now, post_id=action.post_id)
        if kind == self.A_DRAFT_DELETE and action.post_id is not None:
            return self.draft_delete_ask(now=now, post_id=action.post_id)
        if kind == self.A_DRAFT_DELETE_YES and action.post_id is not None:
            return self.draft_delete_yes(now=now, post_id=action.post_id)
        if kind == self.A_DRAFT_SCHEDULE and action.post_id is not None:
            return self.draft_schedule_ask(now=now, post_id=action.post_id)
        if kind == self.A_SCHED_NEXT:
            return self.sched_next(now=now, action=action)
        if kind == self.A_SCHED_OWN:
            return self.sched_own(now=now, action=action)
        if kind == self.A_SCHED_RECENT:
            return self.sched_recent(now=now, action=action)
        if kind == self.A_CONFIRM:
            return self.confirm_schedule(now=now, action=action)
        if kind == self.A_CONFIRM_TIME_EDIT:
            return self.confirm_time_edit(now=now, action=action)
        if kind == self.A_CONFIRM_REPLAN:
            return self.confirm_replan(now=now, action=action)
        if kind == self.A_PLAN:
            return self.plan_today(now=now)
        if kind == self.A_DAY:
            day_iso, page = self._day_arg(action.arg)
            return self.plan_day(now=now, day_iso=day_iso, page=page)
        if kind == self.A_PUB_OPEN and action.publication_id is not None:
            if action.arg is None:
                return self.pub_open(now=now, pub_id=action.publication_id)
            pub = self._publications.get(action.publication_id)
            if pub.status is Status.SENDING:
                return self.pub_open(now=now, pub_id=pub.id)
            # Leave the dashboard before awaiting the preview, so its periodic
            # refresh cannot publish a competing menu card during this action.
            self._go(State.PUBLICATION, {"pub_id": pub.id}, now=now)
            return await self.pub_preview(now=now, pub_id=pub.id)
        if kind == self.A_PUB_PREVIEW and action.publication_id is not None:
            return await self.pub_preview(now=now, pub_id=action.publication_id)
        if kind == self.A_PUB_PAUSE and action.publication_id is not None:
            return self.pub_pause(now=now, action=action)
        if kind == self.A_PUB_EDIT_CONTENT and action.publication_id is not None:
            return self.pub_edit_content(now=now, action=action)
        if kind == self.A_PUB_EDIT_TIME and action.publication_id is not None:
            return self.pub_edit_time(now=now, action=action)
        if kind == self.A_PUB_RESUME and action.publication_id is not None:
            return self.pub_resume(now=now, action=action)
        if kind == self.A_PUB_CANCEL and action.publication_id is not None:
            return self.pub_cancel_ask(now=now, action=action)
        if kind == self.A_PUB_CANCEL_YES and action.publication_id is not None:
            return self.pub_cancel_yes(now=now, action=action)
        if kind == self.A_PUB_DELETE and action.post_id is not None:
            return self.pub_delete_ask(now=now, action=action)
        if kind == self.A_PUB_DELETE_YES and action.post_id is not None:
            return self.pub_delete_yes(now=now, action=action)
        if kind == self.A_PUB_REPLAN and action.publication_id is not None:
            return self.pub_replan(now=now, action=action)
        if kind == self.A_MARK_PUBLISHED and action.publication_id is not None:
            return self.mark_published_ask(now=now, action=action)
        if kind == self.A_TO_DRAFT_RISK and action.publication_id is not None:
            return self.to_draft_risk_ask(now=now, action=action)
        if kind == self.A_ACK_RISK and action.publication_id is not None:
            return self.ack_risk(now=now, action=action)
        if kind == self.A_SETTINGS:
            return self.settings_view(now=now)
        if kind in (self.A_SET_TZ, self.A_SET_START, self.A_SET_END):
            return self.settings_edit(now=now, action=action)
        if kind == self.A_JOURNAL:
            return self.journal_view(now=now, page=self._page_of(action.arg))
        if kind == self.A_BACK:
            self._state.reset_to_menu(now_epoch=now)
            return self._menu_reply(now)
        return Reply(texts.BUTTON_STALE, (), stale_callback=True)

    @staticmethod
    def _page_of(arg: str | None) -> int:
        if isinstance(arg, str) and arg.startswith("p"):
            try:
                return max(0, int(arg[1:]))
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _day_arg(arg: str | None) -> tuple[str, int]:
        if isinstance(arg, str) and ":" in arg:
            day_iso, _, page = arg.partition(":")
            try:
                return day_iso, max(0, int(page))
            except ValueError:
                return day_iso, 0
        return "", 0
