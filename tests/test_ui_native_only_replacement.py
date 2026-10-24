"""Native-only replacement consent survives intake, decisions and restart."""
import asyncio

import pytest

from conftest import NOW
from test_live_composition import live_config, make_bot, OWNER, CHANNEL
from test_ui_forward_intake import Intake, ORIGIN, PHOTO
from test_ui_original_forward import NativeSession, labels
from poster_bot.domain.original_forward import (
    OriginalForwardInput, OriginalForwardSource, build_original_forward_plan,
)
from poster_bot.domain.time import Instant
from poster_bot.live import compose_live
from poster_bot.repo.plans import PlanStore
from poster_bot.ui import texts
from poster_bot.ui.state import UiStateRepo


async def stale_press(h, token):
    from aiogram.types import Update
    from poster_bot.ui.navigation import NavigationStore
    from test_ui_handlers_offline import _raw_message
    h.seq += 1
    card_id = NavigationStore(h.live.app.conn, OWNER).get(OWNER)
    await h.live.dispatcher.feed_update(h.live.bot, Update.model_validate({
        'update_id': h.seq, 'callback_query': {
            'id': f'stale-{h.seq}', 'from': {'id': OWNER, 'is_bot': False, 'first_name': 'Owner'},
            'chat_instance': 'ci', 'data': token,
            'message': _raw_message(message_id=card_id, text='card')}}))


@pytest.mark.parametrize('scheduled', [False, True])
@pytest.mark.parametrize('kind', ['text', 'photo'])
@pytest.mark.parametrize('decision', [None, 'keep', 'replace'])
def test_native_only_rejects_ordinary_input_without_capturing_or_losing_state(
    tmp_path, scheduled, kind, decision,
):
    async def scenario():
        config = live_config(tmp_path)
        session = NativeSession()
        live = await compose_live(config, clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            logical = OriginalForwardInput(OriginalForwardSource(OWNER, (30,)))
            plan = build_original_forward_plan(logical, owner_user_id=OWNER, channel_id=CHANNEL)
            pid = live.app.drafts.create_with_plan(logical, plan, now_epoch=NOW)
            pub = live.app.publications.schedule(pid, Instant(NOW + 3600), now_epoch=NOW) if scheduled else None
            await h.message(message_id=2, text=texts.MENU_PLAN if scheduled else texts.MENU_DRAFTS)
            await h.press(labels(h)[0])
            await h.press(texts.BTN_EDIT_CONTENT)
            signed_token = next(b['callback_data'] for row in
                h.card()['reply_markup']['inline_keyboard'] for b in row
                if b['text'] == 'Заменить копией с подписью')
            await h.press('Заменить другой пересылкой как есть')
            chosen = UiStateRepo(live.app.conn).get()
            await stale_press(h, signed_token)
            assert UiStateRepo(live.app.conn).get() == chosen

            async def refuse(mid):
                before = UiStateRepo(live.app.conn).get()
                await h.message(message_id=mid, **({'text': 'not consent'} if kind == 'text'
                                                  else {'photo': PHOTO, 'caption': 'not consent'}))
                assert UiStateRepo(live.app.conn).get() == before
                assert not UiStateRepo(live.app.conn).has_effect(f'{OWNER}:{mid}')
                assert not UiStateRepo(live.app.conn).has_effect(f'discarded:{OWNER}:{mid}')
                assert not h.deleted(mid)
                assert PlanStore(live.app.conn).get(pid).plan == plan
                if pub:
                    assert live.app.publications.get(pub.id) == pub

            await refuse(39)
            if decision:
                await h.message(message_id=40, forward_origin=ORIGIN, text='first source')
                await h.message(message_id=41, forward_origin=ORIGIN, text='second source')
                await refuse(42)
                pending = UiStateRepo(live.app.conn).get()
                await live.close()
                live = await compose_live(config, clock=lambda: NOW,
                                          bot=make_bot(session), now_epoch=NOW)
                h = Intake(live, session)
                assert UiStateRepo(live.app.conn).get() == pending
                await refuse(45)
                await h.press(texts.BTN_KEEP if decision == 'keep' else texts.BTN_REPLACE)
                await refuse(43)

            # Durable mode, not a transient flag on the choice card.
            await live.close()
            live = await compose_live(config, clock=lambda: NOW,
                                      bot=make_bot(session), now_epoch=NOW)
            h = Intake(live, session)
            await refuse(44)
            if not decision:
                await h.message(message_id=40, forward_origin=ORIGIN, text='native source')
            await h.press(texts.BTN_CONTINUE)
            assert texts.BTN_COPY_SIGNED not in labels(h)
            await h.press(texts.BTN_FORWARD_AS_IS)
            await h.press(texts.BTN_CONTINUE)
            assert PlanStore(live.app.conn).get(pid).plan == plan
            await h.press(texts.BTN_CONFIRM)
            stored = PlanStore(live.app.conn).get(pid)
            assert stored.plan.version == 4
            assert stored.plan.source.message_ids == ((41,) if decision == 'replace' else (40,))
            assert live.app.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == 1
            assert not h.deleted(30)
        finally:
            await live.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('scheduled', [False, True])
@pytest.mark.parametrize('kind', ['text', 'photo'])
def test_cancel_native_replacement_then_explicit_signed_choice_allows_ordinary_input(tmp_path, scheduled, kind):
    async def scenario():
        session = NativeSession()
        live = await compose_live(live_config(tmp_path), clock=lambda: NOW,
                                  bot=make_bot(session), now_epoch=NOW)
        h = Intake(live, session)
        try:
            logical = OriginalForwardInput(OriginalForwardSource(OWNER, (30,)))
            plan = build_original_forward_plan(logical, owner_user_id=OWNER, channel_id=CHANNEL)
            pid = live.app.drafts.create_with_plan(logical, plan, now_epoch=NOW)
            pub = live.app.publications.schedule(pid, Instant(NOW + 3600), now_epoch=NOW) if scheduled else None
            await h.message(message_id=2, text=texts.MENU_PLAN if scheduled else texts.MENU_DRAFTS)
            await h.press(labels(h)[0])
            await h.press(texts.BTN_EDIT_CONTENT)
            await h.press('Заменить другой пересылкой как есть')
            await h.message(message_id=40, forward_origin=ORIGIN, text='abandoned replacement')
            await h.press(texts.BTN_CANCEL)
            assert PlanStore(live.app.conn).get(pid).plan == plan
            if pub:
                assert live.app.publications.get(pub.id) == pub
            await h.press(texts.BTN_EDIT_CONTENT)
            await h.press('Заменить копией с подписью')
            await h.message(message_id=41, **({'text': 'signed replacement'} if kind == 'text'
                                              else {'photo': PHOTO, 'caption': 'signed replacement'}))
            assert PlanStore(live.app.conn).get(pid).plan == plan
            await h.press(texts.BTN_CONTINUE)
            assert PlanStore(live.app.conn).get(pid).plan == plan
            assert texts.BTN_CONFIRM in labels(h)
            await h.press(texts.BTN_CONFIRM)
            stored = PlanStore(live.app.conn).get(pid)
            assert stored.plan.version == 3
            assert stored.logical.body == 'signed replacement'
            assert bool(stored.logical.media) == (kind == 'photo')
            assert not h.deleted(30)
        finally:
            await live.close()
    asyncio.run(scenario())
