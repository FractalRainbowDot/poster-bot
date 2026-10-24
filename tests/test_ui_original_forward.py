"""Configured Dispatcher native consent, review, scheduling and channel dispatch."""
import asyncio
import json

import pytest
from aiogram.exceptions import TelegramNetworkError

from conftest import NOW
from test_live_composition import LiveApiSession, live_config, make_bot, OWNER, CHANNEL
from test_ui_forward_intake import Intake, ORIGIN, PHOTO
from poster_bot.live import compose_live
from poster_bot.repo.plans import PlanStore
from poster_bot.ui import texts
from poster_bot.ui.state import UiStateRepo


class NativeSession(LiveApiSession):
    partial = False
    fail_forward = False
    fail_controls = False

    async def make_request(self, bot, method, timeout=None):
        if (self.fail_controls and method.__api_method__ in ('sendMessage', 'editMessageText')
                and getattr(method, 'reply_markup', None)):
            raise TelegramNetworkError(method=method, message='isolated controls failure')
        if method.__api_method__ == 'forwardMessages':
            p = method.model_dump(mode='json', exclude_none=True, fallback=str)
            p = {key: p[key] for key in ('chat_id', 'from_chat_id', 'message_ids')}
            self.requests.append(('forwardMessages', p))
            if self.fail_forward:
                raise TelegramNetworkError(method=method, message='isolated timeout')
            count = len(p['message_ids']) - int(self.partial)
            result = []
            for _ in range(count):
                raw = self._raw()
                if not hasattr(self, 'forward_acks'):
                    self.forward_acks = []
                self.forward_acks.append(raw['message_id'])
                self.visible[raw['message_id']] = {'forwarded': True}
                result.append({'message_id': raw['message_id']})
            return self.check_response(bot=bot, method=method, status_code=200,
                content=json.dumps({'ok': True, 'result': result})).result
        return await super().make_request(bot, method, timeout)


def labels(h):
    return [b['text'] for row in h.card()['reply_markup']['inline_keyboard'] for b in row]


@pytest.mark.parametrize('kind,count', [('text', 1), ('photo', 1), ('video', 1), ('photo', 3)])
def test_native_creation_full_preview_schedule_dispatch(tmp_path, kind, count):
    async def scenario():
        session = NativeSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            await h.message(message_id=2, text=texts.MENU_NEW_POST)
            for n in range(count):
                payload = dict(message_id=40+n, forward_origin=ORIGIN)
                if kind == 'text':
                    payload.update(text='source text', entities=[
                        {'type': 'bold', 'offset': 0, 'length': 6}])
                elif kind == 'photo':
                    payload.update(photo=PHOTO, caption='original caption')
                else:
                    payload.update(video={'file_id': 'video', 'file_unique_id': 'v',
                        'width': 100, 'height': 100, 'duration': 1}, caption='video caption')
                if count > 1:
                    payload['media_group_id'] = 'album'
                await h.message(**payload)
            assert texts.BTN_FORWARD_AS_IS in labels(h)
            assert texts.BTN_COPY_SIGNED in labels(h)
            assert not [p for m, p in session.requests if m == 'forwardMessages']
            await h.press(texts.BTN_FORWARD_AS_IS)
            forwards = [p for m, p in session.requests if m == 'forwardMessages']
            assert forwards == [dict(chat_id=OWNER, from_chat_id=OWNER,
                                     message_ids=list(range(40, 40+count)))], (h.card(), UiStateRepo(live.app.conn).get())
            assert not live.app.conn.execute('SELECT 1 FROM posts').fetchone()
            assert all(not h.deleted(40+n) for n in range(count))
            await h.press(texts.BTN_CONTINUE)
            pid = UiStateRepo(live.app.conn).get().arg('post_id')
            stored = PlanStore(live.app.conn).get(pid)
            assert stored.plan.version == 4
            assert stored.plan.source.message_ids == tuple(range(40, 40+count))
            assert stored.plan.owner_user_id == OWNER
            assert stored.plan.channel_id == CHANNEL
            assert json.loads(stored.logical.to_json())['v'] == 3
            slot = next(label for label in labels(h) if ' → ' in label)
            await h.press(slot)
            await h.press(texts.BTN_CONFIRM)
            pub = live.app.conn.execute('SELECT * FROM publications').fetchone()
            report = await live.runtime.tick(now_epoch=pub['scheduled_at'] if
                isinstance(pub['scheduled_at'], int) else live.app.publications.get(pub['id']).scheduled_epoch)
            assert [r.outcome for r in report.sent] == ['published']
            forwards = [p for m, p in session.requests if m == 'forwardMessages']
            assert len(forwards) == 2
            assert forwards[-1] == dict(chat_id=CHANNEL, from_chat_id=OWNER,
                message_ids=list(range(40, 40+count)))
            assert all(not h.deleted(40+n) for n in range(count))
        finally:
            await live.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('reference', ['plan', 'intent', 'corrupt_plan', 'corrupt_intent'])
def test_cleanup_guard_includes_retained_native_plan_and_intent(services, reference):
    from test_original_forward_dispatch import native_publication
    from ui_flow_helpers import build_flow
    from planned_helpers import SIG
    from poster_bot.domain.content import Content
    pub, plan = native_publication(services)
    if 'intent' in reference:
        services.dispatch.claim(pub.id, pub.version, now_epoch=pub.scheduled_epoch,
                                owner_user_id=123, channel_id=-100999)
        services.conn.execute('DELETE FROM post_plans WHERE post_id=?', (pub.post_id,))
        services.conn.execute('UPDATE posts SET content_json=? WHERE id=?',
                             (Content(kind='text', text='now a copy').to_json(), pub.post_id))
    if reference == 'corrupt_plan':
        services.conn.execute("UPDATE post_plans SET plan_json='{}'")
    if reference == 'corrupt_intent':
        services.conn.execute("UPDATE publication_parts SET part_snapshot_json='{}'")
    flow, _ = build_flow(services, signature=SIG)
    assert flow.forward_source_referenced(chat_id=123, message_id=11)
    if reference.startswith('corrupt'):
        assert flow.forward_source_referenced(chat_id=123, message_id=999)


def test_unrelated_forward_does_not_join_album():
    from poster_bot.ui.staged import Staged, copy_candidate_from_text
    staged = Staged()
    candidate = copy_candidate_from_text('first', ())
    assert staged.add_forward(OWNER, 40, None, candidate) == 'staged'
    assert staged.add_forward(OWNER, 41, 'new-album', candidate) == 'conflict'
    assert staged.forward_ids == [40]


@pytest.mark.parametrize('scheduled', [False, True])
@pytest.mark.parametrize('source', ['native', 'raw', 'signed'])
@pytest.mark.parametrize('failure', ['none', 'partial', 'controls'])
def test_native_replacement_requires_full_preview_then_separate_confirmation(tmp_path, scheduled, source, failure):
    async def scenario():
        from poster_bot.domain.original_forward import (OriginalForwardInput,
            OriginalForwardSource, build_original_forward_plan)
        from poster_bot.domain.time import Instant
        session = NativeSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            logical = OriginalForwardInput(OriginalForwardSource(OWNER, (30,)))
            plan = build_original_forward_plan(logical, owner_user_id=OWNER, channel_id=CHANNEL)
            if source == 'raw':
                from poster_bot.domain.original_forward import plan_primary_content
                pid = live.app.drafts.create(plan_primary_content(plan), now_epoch=NOW)
                plan = None
            elif source == 'signed':
                from planned_helpers import SIG
                from poster_bot.domain.planner import LogicalPost, plan_post
                logical = LogicalPost(body='signed body')
                plan = plan_post(logical, SIG).plan
                pid = live.app.drafts.create_with_plan(logical, plan, now_epoch=NOW)
            else:
                pid = live.app.drafts.create_with_plan(logical, plan, now_epoch=NOW)
            before = dict(live.app.conn.execute('SELECT * FROM posts WHERE id=?', (pid,)).fetchone())
            pub = live.app.publications.schedule(pid, Instant(NOW + 3600), now_epoch=NOW) if scheduled else None
            await h.message(message_id=2, text=texts.MENU_PLAN if scheduled else texts.MENU_DRAFTS)
            await h.press(labels(h)[0])
            if source == 'raw':
                await h.press(texts.BTN_FORWARD_RESEND)
            else:
                await h.press(texts.BTN_EDIT_CONTENT)
                await h.press('Заменить другой пересылкой как есть' if source == 'native'
                              else texts.BTN_EDIT_ALL)
            for mid in (40, 41):
                await h.message(message_id=mid, forward_origin=ORIGIN, photo=PHOTO,
                                media_group_id='replacement')
            session.partial = failure == 'partial'
            session.fail_controls = failure == 'controls'
            if failure == 'controls':
                with pytest.raises(TelegramNetworkError):
                    await h.press(texts.BTN_FORWARD_AS_IS)
            else:
                await h.press(texts.BTN_FORWARD_AS_IS)
            stored = PlanStore(live.app.conn).get(pid)
            assert (stored.plan if stored else None) == plan
            if failure != 'none':
                from poster_bot.ui.navigation import NavigationStore
                ui = UiStateRepo(live.app.conn).get()
                assert NavigationStore(live.app.conn, OWNER).lifecycle.approved_review(f'staged:{ui.version}') is None
                frozen = ui.arg('native_plan')
                forwards_before = len([p for m, p in session.requests if m == 'forwardMessages'])
                session.partial = session.fail_controls = False
                await h.message(message_id=41, forward_origin=ORIGIN, photo=PHOTO,
                                media_group_id='replacement')
                assert len([p for m, p in session.requests if m == 'forwardMessages']) == forwards_before
                assert texts.BTN_CONTINUE not in labels(h)
                if pub:
                    assert live.app.publications.get(pub.id) == pub
                await h.press('Повторить предпросмотр')
                assert UiStateRepo(live.app.conn).get().arg('native_plan') == frozen
            await h.press(texts.BTN_CONTINUE)
            assert texts.BTN_CONFIRM in labels(h)
            stored = PlanStore(live.app.conn).get(pid)
            assert (stored.plan if stored else None) == plan
            if pub:
                assert live.app.publications.get(pub.id) == pub
            await h.press(texts.BTN_CONFIRM)
            stored = PlanStore(live.app.conn).get(pid)
            assert stored.plan.version == 4
            assert stored.plan.source.message_ids == (40, 41)
            after = dict(live.app.conn.execute('SELECT * FROM posts WHERE id=?', (pid,)).fetchone())
            assert (after['id'], after['created_at']) == (before['id'], before['created_at'])
            assert live.app.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == 1
            if pub:
                updated = live.app.publications.get(pub.id)
                assert (updated.id, updated.post_id, updated.minute_epoch) == (pub.id, pid, pub.minute_epoch)
                assert updated.version == pub.version + 1
                assert updated.status.value == 'suspended'
            assert not h.deleted(40) and not h.deleted(41)
        finally:
            await live.close()
    asyncio.run(scenario())


def test_native_controls_failure_does_not_approve_preview(tmp_path):
    async def scenario():
        from poster_bot.ui.navigation import NavigationStore
        session = NativeSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            await h.message(message_id=2, text=texts.MENU_NEW_POST)
            await h.message(message_id=40, forward_origin=ORIGIN, text='source')
            session.fail_controls = True
            with pytest.raises(TelegramNetworkError):
                await h.press(texts.BTN_FORWARD_AS_IS)
            ui = UiStateRepo(live.app.conn).get()
            lifecycle = NavigationStore(live.app.conn, OWNER).lifecycle
            assert lifecycle.approved_review(f'staged:{ui.version}') is None
            assert not live.app.conn.execute('SELECT 1 FROM posts').fetchone()
            assert not h.deleted(40)
            session.fail_controls = False
            await h.message(message_id=40, forward_origin=ORIGIN, text='source')
            assert texts.BTN_CONTINUE not in labels(h)
            assert 'Повторить предпросмотр' in labels(h)
            assert len([p for m, p in session.requests if m == 'forwardMessages']) == 1
        finally:
            await live.close()
    asyncio.run(scenario())


def test_native_preview_ack_colliding_with_source_is_never_deleted_or_approved(tmp_path):
    class CollisionSession(NativeSession):
        async def make_request(self, bot, method, timeout=None):
            if method.__api_method__ == 'forwardMessages':
                return self.check_response(bot=bot, method=method, status_code=200,
                    content=json.dumps({'ok': True, 'result': [{'message_id': 40}]})).result
            return await super().make_request(bot, method, timeout)

    async def scenario():
        from poster_bot.ui.navigation import NavigationStore
        session = CollisionSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            await h.message(message_id=2, text=texts.MENU_NEW_POST)
            await h.message(message_id=40, forward_origin=ORIGIN, text='protected source')
            await h.press(texts.BTN_FORWARD_AS_IS)
            ui = UiStateRepo(live.app.conn).get()
            assert NavigationStore(live.app.conn, OWNER).lifecycle.approved_review(f'staged:{ui.version}') is None
            assert texts.BTN_CONTINUE not in labels(h)
            assert not h.deleted(40)
            assert not live.app.conn.execute('SELECT 1 FROM posts').fetchone()
        finally:
            await live.close()
    asyncio.run(scenario())


def test_native_to_signed_conversion_needs_explicit_choice_preview_and_confirm(tmp_path):
    async def scenario():
        from poster_bot.domain.original_forward import (OriginalForwardInput,
            OriginalForwardSource, build_original_forward_plan)
        session = NativeSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            logical = OriginalForwardInput(OriginalForwardSource(OWNER, (30,)))
            plan = build_original_forward_plan(logical, owner_user_id=OWNER, channel_id=CHANNEL)
            pid = live.app.drafts.create_with_plan(logical, plan, now_epoch=NOW)
            await h.message(message_id=2, text=texts.MENU_DRAFTS)
            await h.press(labels(h)[0])
            await h.press(texts.BTN_EDIT_CONTENT)
            assert texts.BTN_EDIT_TEXT not in labels(h)
            assert texts.BTN_EDIT_ATTACHMENT not in labels(h)
            await h.message(message_id=39, text='not consent')
            assert PlanStore(live.app.conn).get(pid).plan == plan
            await h.press('Заменить копией с подписью')
            await h.message(message_id=40, forward_origin=ORIGIN, text='copied body')
            await h.press(texts.BTN_COPY_SIGNED)
            assert PlanStore(live.app.conn).get(pid).plan == plan
            assert not h.deleted(30)
            await h.press(texts.BTN_CONTINUE)
            assert PlanStore(live.app.conn).get(pid).plan == plan
            assert texts.BTN_CONFIRM in labels(h)
            await h.press(texts.BTN_CONFIRM)
            stored = PlanStore(live.app.conn).get(pid)
            assert stored.plan.version == 3
            assert stored.logical.body == 'copied body'
        finally:
            await live.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['timeout', 'partial'])
def test_native_failed_preview_never_creates_post_and_explicit_retry(tmp_path, failure):
    async def scenario():
        session = NativeSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            await h.message(message_id=2, text=texts.MENU_NEW_POST)
            for mid in (40, 41):
                await h.message(message_id=mid, forward_origin=ORIGIN, photo=PHOTO,
                                media_group_id='album')
            session.fail_forward = failure == 'timeout'
            session.partial = failure == 'partial'
            await h.press(texts.BTN_FORWARD_AS_IS)
            assert texts.BTN_CONTINUE not in labels(h)
            assert not live.app.conn.execute('SELECT 1 FROM posts').fetchone()
            snapshot = UiStateRepo(live.app.conn).get().arg('native_plan')
            assert snapshot
            assert not h.deleted(40) and not h.deleted(41)
            if failure == 'partial':
                partial_ids = session.forward_acks
                assert partial_ids
                assert all(h.deleted(mid) for mid in partial_ids)
            session.fail_forward = session.partial = False
            await h.press('Повторить предпросмотр')
            assert UiStateRepo(live.app.conn).get().arg('native_plan') == snapshot
            await h.press(texts.BTN_CONTINUE)
            assert live.app.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == 1
        finally:
            await live.close()
    asyncio.run(scenario())
