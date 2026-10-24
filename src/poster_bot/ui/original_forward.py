"""Explicit native-forward consent and private review, before durable creation."""
from __future__ import annotations

import hashlib
from dataclasses import replace

from ..domain.original_forward import (
    OriginalForwardInput, OriginalForwardPlan,
    plan_primary_content,
)
from ..transport.ports import Sent, UncertainOutcome
from . import texts
from .replies import Reply
from .state import State


class OriginalForwardFlow:
    def _native_review_reply(self, *, now, retry=False):
        ui = self._ui()
        rows = []
        if retry:
            rows.append((self._btn('Повторить предпросмотр', self.A_STAGED_PREVIEW,
                                   now=now, expected_version=ui.version),))
        else:
            confirm = ui.state is State.EDITING and ui.arg('edit_confirm')
            rows.append((self._btn(texts.BTN_CONFIRM if confirm else texts.BTN_CONTINUE,
                                   self.A_EDIT_CONFIRM if confirm else self.A_CONTINUE,
                                   now=now, expected_version=ui.version),))
        rows.append((self._btn(texts.BTN_CANCEL, self.A_BACK, now=now),))
        return Reply('Пересылка как есть, без нашей подписи. Источник сохранён — '
                     'не удаляйте его до публикации.' if not retry else
                     'Не удалось подтвердить полный предпросмотр. Источник сохранён. '
                     'Повторите предпросмотр отдельной кнопкой.', tuple(rows))

    def _native_pair(self, ui):
        logical = OriginalForwardInput.from_json(ui.arg('native_input'))
        plan = OriginalForwardPlan.from_json(ui.arg('native_plan'))
        if self._navigation is None:
            raise ValueError('trusted owner binding unavailable')
        plan.validate_binding(owner_user_id=self._navigation.owner_id,
                              channel_id=self._channel_id)
        staged = self._staged(ui)
        accepted = OriginalForwardInput.from_accepted_messages(
            self._navigation.owner_id, tuple((staged.forward_chat_id, mid,
                staged.forward_group_id) for mid in staged.forward_ids))
        if accepted != logical or plan.source != logical.source:
            raise ValueError('native source snapshot changed')
        return logical, plan

    async def _review_native_staged(self, reply, *, now):
        ui = self._ui()
        lifecycle = self._navigation.lifecycle if self._navigation else None
        key = f'staged:{ui.version}'
        try:
            logical, plan = self._native_pair(ui)
            review = {'logical': logical.to_json(), 'plan': plan.to_json()}
            if ui.state is State.EDITING:
                if not self._edit_target_current(ui):
                    raise ValueError('edit target changed')
                review['identity'] = self._edit_identity(ui)
                if self._approved_edit(ui) == review:
                    return reply, True
            if lifecycle is None:
                raise ValueError('durable preview lifecycle unavailable')
            if lifecycle.approved_review(key) == review:
                return reply, True
            lifecycle.prepare_review(key, review, now=now)
            outcome = await self._send_preview_plan(plan, now=now)
            if self._ui().version != ui.version:
                return replace(reply, stale_callback=True), False
            if isinstance(outcome, Sent):
                lifecycle.bind_candidate(key, now=now,
                    snapshot=hashlib.sha256(plan.to_json().encode()).hexdigest(),
                    review=review)
                return reply, True
        except Exception:
            pass  # no source coordinates or transport internals in owner errors
        if lifecycle is not None:
            lifecycle.failed_candidate(now=now)
        return self._native_review_reply(now=now, retry=True), False

    async def _send_native_preview(self, plan, *, now):
        if self._navigation is None:
            raise ValueError('trusted owner binding unavailable')
        plan.validate_binding(owner_user_id=self._navigation.owner_id,
                              channel_id=self._channel_id)
        outcome = await self._preview.send_preview(plan_primary_content(plan))
        if isinstance(outcome, (Sent, UncertainOutcome)):
            ids = (outcome.message_ids if isinstance(outcome, Sent)
                   else outcome.confirmed_message_ids)
            complete = (isinstance(outcome, Sent)
                        and len(ids) == len(plan.source.message_ids)
                        and all(type(mid) is int and mid > 0 for mid in ids)
                        and len(set(ids)) == len(ids))
            # Only newly acknowledged BOT outputs enter this ledger, never input.
            safe_ids = tuple(dict.fromkeys(mid for mid in ids
                if type(mid) is int and mid > 0 and not self.forward_source_referenced(
                    chat_id=self._navigation.owner_id, message_id=mid)))
            self._navigation.lifecycle.stage(safe_ids, now=now,
                complete=complete and len(safe_ids) == len(ids))
            if isinstance(outcome, Sent) and (not complete or len(safe_ids) != len(ids)):
                return UncertainOutcome(code='partial_result')
        return outcome

    async def _continue_native(self, *, now, ui, action):
        try:
            logical, plan = self._native_pair(ui)
            review = {'logical': logical.to_json(), 'plan': plan.to_json()}
            if ui.state is State.EDITING:
                review['identity'] = self._edit_identity(ui)
                approved = self._approved_edit(ui)
            else:
                approved = self._navigation.lifecycle.approved_review(f'staged:{ui.version}')
            if approved != review:
                return self._native_review_reply(now=now, retry=True)
        except Exception:
            return self._native_review_reply(now=now, retry=True)
        if ui.state is State.EDITING:
            if not ui.arg('edit_confirm'):
                self._go(State.EDITING, dict(ui.payload, edit_confirm=True,
                         edit_review_version=ui.version), now=now, expected_version=ui.version)
                return replace(self._native_review_reply(now=now),
                               text='Подтвердить изменения публикации?',
                               staged_preview_version=self._ui().version)
            if action.action != self.A_EDIT_CONFIRM:
                return self._replay_reply(now=now)
            return await self._commit_planned_edit(now=now, ui=ui, logical=logical, plan=plan)
        post_id = self._drafts.create_with_plan(logical, plan, now_epoch=now)
        self._journal.record('draft_created', post_id=post_id, now_epoch=now)
        self._reuse_staged_review(post_id, ui.version, plan, now=now)
        return self._draft_created_reply(now=now, post_id=post_id, plan=plan)
