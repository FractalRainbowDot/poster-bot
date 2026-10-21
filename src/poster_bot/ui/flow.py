"""The owner conversation engine: every owner-facing decision, validated
against durable state, with Russian replies and token-issued buttons.

Transport-free by design: the aiogram adapter normalizes updates, enforces
the owner/private guard, and calls these methods; tests drive the same
methods directly. No method here trusts a stale step: the durable
conversation version, the publication row version, "strictly future" and
minute uniqueness are re-checked at execution inside the domain services.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date, timedelta
from typing import Protocol

from ..domain.content import Content, ContentValidationError, Entity
from ..domain.errors import (
    ConflictError,
    DomainError,
    MinuteTakenError,
    NonLocalTimeError,
    NoSlotAvailableError,
    AmbiguousLocalTimeError,
    PastTimeError,
    TransitionError,
)
from ..domain.states import Status
from ..domain.time import Instant, next_free_slot, recent_manual_suggestions
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
from .replies import Button, MAX_ROWS, PAGE_SIZE, Reply, nav_rows, page_window
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


class OwnerFlow:
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
    A_DRAFT_PREVIEW = "draft_preview"
    A_DRAFT_EDIT = "draft_edit"
    A_DRAFT_DELETE = "draft_delete"
    A_DRAFT_DELETE_YES = "draft_delete_yes"
    A_DRAFT_SCHEDULE = "draft_schedule"
    A_PLAN = "plan"
    A_DAY = "day"
    A_PUB_OPEN = "pub_open"
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
        self._channel_id = channel_id
        self._state = state or UiStateRepo(conn)
        self._actions = actions or ActionStore(conn)

    # ------------------------------------------------------------------ core
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
        return Button(label, self._token(action, now=now, **kw))

    def _menu_reply(self, now: int) -> Reply:
        rows = tuple(
            (self._btn(label, act, now=now),)
            for label, act in (
                (texts.MENU_NEW_POST, self.A_NEW),
                (texts.MENU_DRAFTS, self.A_DRAFTS),
                (texts.MENU_PLAN, self.A_PLAN),
                (texts.MENU_SETTINGS, self.A_SETTINGS),
                (texts.MENU_JOURNAL, self.A_JOURNAL),
            )
        )
        return Reply(texts.MENU_TEXT, rows)

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
        return await handler(now=now, ui=ui, text=text, entities=entities)

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
                self._state.set(ui.state, self._staged_payload(staged, ui),
                                expected_version=ui.version, now_epoch=now,
                                effect_key=source_key)
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
            f"В черновике: {label} ({count}). {texts.BTN_CONTINUE} — когда всё "
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
        copy = staged.as_copy()
        self._state.set(ui.state, {"staged": copy.to_dict()},
                        expected_version=ui.version, now_epoch=now)
        reply = self._staged_reply(now=now, staged=copy)
        return Reply(f"{texts.COPY_READY} {reply.text}", reply.rows)

    # ------------------------------------------------------------------- drafts
    def drafts_view(self, *, now: int, page: int) -> Reply:
        ids = self._drafts.list_drafts()
        start, end, pages = page_window(len(ids), page)
        if not ids:
            self._go(State.MENU, {"view": "drafts"}, now=now)
            return Reply(texts.NO_DRAFTS, ((self._btn(texts.BTN_BACK, self.A_BACK, now=now),),))
        rows = []
        for pid in ids[start:end]:
            try:
                line = self._content_line(self._drafts.get_content(pid))
            except DraftNotFound:
                line = "—"
            rows.append((self._btn(f"№{pid} · {line}", self.A_DRAFT_OPEN,
                                   now=now, post_id=pid),))
        nav = []
        if page > 0:
            nav.append(self._btn("← Пред.", self.A_DRAFTS, now=now,
                                 arg=f"p{page - 1}"))
        if page < pages - 1:
            nav.append(self._btn("След. →", self.A_DRAFTS, now=now,
                                 arg=f"p{page + 1}"))
        nav.append(self._btn(texts.BTN_BACK, self.A_BACK, now=now))
        rows.append(tuple(nav))
        self._go(State.MENU, {"view": "drafts"}, now=now)
        return Reply(f"{texts.TITLE_DRAFTS} ({start + 1}–{end} из {len(ids)})",
                     tuple(rows[:MAX_ROWS]))

    def draft_open(self, *, now: int, post_id: int) -> Reply:
        return self._draft_view(now=now, post_id=post_id)

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
        return Reply(f"{texts.DRAFT_DELETE_ASK} Черновик №{post_id}.", rows)

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
        text = f"Черновик №{post_id}. Содержимое: {line}."
        if note:
            text = f"{note}\n{text}"
        rows = (
            (self._btn(texts.BTN_SCHEDULE, self.A_DRAFT_SCHEDULE, now=now,
                       post_id=post_id),),
            (self._btn(texts.BTN_PREVIEW, self.A_DRAFT_PREVIEW, now=now,
                       post_id=post_id),),
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
        settings = self._settings.get()
        try:
            instant = next_free_slot(now, settings.timezone,
                                     settings.start_hour, settings.end_hour,
                                     self._publications.reserved_minutes())
        except NoSlotAvailableError:
            return Reply(texts.NO_SLOT,
                         self._schedule_picker(now=now, post_id=post_id).rows)
        return self._confirm_schedule_view(now=now, post_id=post_id,
                                           instant=instant)

    def sched_own(self, *, now: int, action: Action) -> Reply:
        ui = self._ui()
        post_id = action.post_id
        if ui.state is not State.DRAFT or ui.arg("post_id") != post_id:
            self._go(State.MENU, {}, now=now)
            return Reply(texts.STEP_GONE, self._menu_reply(now).rows)
        settings = self._settings.get()
        self._go(State.MANUAL_TIME, {"purpose": "schedule", "post_id": post_id},
                 now=now)
        return self._time_ask_reply(now=now, settings=settings,
                                    back=Button(texts.BTN_BACK, self._token(
                                        self.A_DRAFT_OPEN, now=now,
                                        post_id=post_id)))

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
                            minute_epoch=instant.epoch)
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
        try:
            pub = self._publications.schedule(post_id, Instant(minute),
                                              now_epoch=now)
        except (MinuteTakenError, PastTimeError, ConflictError,
                DomainError) as exc:
            reason = self._schedule_refusal_ru(exc)
            if isinstance(exc, (MinuteTakenError, PastTimeError)):
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
            return self._confirm_schedule_view(now=now,
                                               post_id=int(ui.arg("post_id")),
                                               instant=instant, manual=(hh, mm))
        if purpose in ("edit_time", "replan"):
            return self._confirm_minute_change_view(now=now, ui=ui,
                                                    instant=instant,
                                                    manual=(hh, mm))
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
                            minute_epoch=instant.epoch)
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
        start_i, end_i, pages = page_window(len(pubs), page)
        rows = []
        for pub in pubs[start_i:end_i]:
            row = [self._btn(
                f"{self._pub_time_local(pub)} · {texts.STATUS_RU[pub.status]}"
                f" · №{pub.id}",
                self.A_PUB_OPEN, now=now, publication_id=pub.id,
            )]
            if pub.status is Status.PUBLISHED:
                link = self._last_permalink(pub.post_id)
                if link:
                    row.append(Button(texts.BTN_LINK, url=link))
            rows.append(tuple(row))
        rows.extend(self._day_nav_rows(now=now, day=day, page=page,
                                       pages=pages))
        self._go(State.MENU, {"view": "day"}, now=now)
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
            add((self._btn(texts.BTN_DELETE, self.A_PUB_DELETE, now=now,
                           publication_id=pub.id, post_id=pub.post_id),))
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
        settings = self._settings.get()
        self._go(State.MANUAL_TIME,
                 {"purpose": "edit_time", "pub_id": pub_id,
                  "expected_version": int(action.expected_version)}, now=now)
        return Reply(f"{texts.TIME_ASK}{settings.timezone}).",
                     ((self._btn(texts.BTN_BACK, self.A_BACK, now=now),),))

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
        settings = self._settings.get()
        self._go(State.MANUAL_TIME,
                 {"purpose": "replan", "pub_id": pub_id,
                  "expected_version": int(action.expected_version)}, now=now)
        return Reply(f"{texts.TIME_ASK}{settings.timezone}).",
                     ((self._btn(texts.BTN_BACK, self.A_BACK, now=now),),))

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
        return self._publication_view(now=now, pub_id=pub_id,
                                      note=texts.MARKED_PUBLISHED)

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
        return self.settings_view(now=now)

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
    async def press(self, *, now: int, token: str) -> Reply:
        try:
            action = self._actions.consume(token, now_epoch=now)
        except ActionStale:
            return Reply(texts.BUTTON_STALE, ())
        return await self._run_action(now=now, action=action)

    async def _run_action(self, *, now: int, action: Action) -> Reply:
        kind = action.action
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
            return self.pub_open(now=now, pub_id=action.publication_id)
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
        return Reply(texts.BUTTON_STALE, ())

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