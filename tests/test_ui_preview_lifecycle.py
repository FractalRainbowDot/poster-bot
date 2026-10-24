"""Visible-chat regressions using real Dispatcher and private preview transport."""
import asyncio
import json
from dataclasses import replace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter

from poster_bot.transport.aiogram_channel import AiogramChannelTransport
from poster_bot.ui import texts
from poster_bot.domain.signature import SignatureConfig
from poster_bot.repo.plans import PlanStore
from poster_bot.ui.preview_lifecycle import PreviewLifecycle
from test_ui_handlers_offline import OWNER, _Harness, _FakeApiSession, _raw_message
from ui_flow_helpers import FLOW_CHANNEL


class VisibleApi(_FakeApiSession):
    def __init__(self):
        super().__init__()
        self.roles = {}
        self.generated = []
        self.delete_errors = {}
        self.navigation = None

    def _raw(self):
        raw = super()._raw()
        self.generated.append(raw["message_id"])
        return raw

    async def make_request(self, bot, method, timeout=None):
        if method.__api_method__ == "deleteMessage":
            error = self.delete_errors.pop(method.message_id, None)
            if error:
                self.requests.append(("deleteMessage", {"chat_id": method.chat_id,
                                                       "message_id": method.message_id}))
                if error == "retry":
                    raise TelegramRetryAfter(method=method, message="limited", retry_after=30)
                if error == "network":
                    raise TelegramNetworkError(method=method, message="offline")
                raise TelegramBadRequest(method=method, message=error)
        self.generated = []
        result = await super().make_request(bot, method, timeout)
        name = method.__api_method__
        payload = self.requests[-1][1]
        if name.startswith("send") or name in ("forwardMessages", "copyMessages"):
            assert payload["chat_id"] == OWNER
            markup = payload.get("reply_markup", {})
            role = ("keyboard" if "keyboard" in markup else
                    "nav" if "inline_keyboard" in markup else "preview")
            for mid in self.generated:
                self.visible[mid] = payload
                self.roles[mid] = role
        return result

    def alive(self, role):
        # An empty main card now legitimately has no inline buttons. Its
        # durable ID, not the presence of markup, distinguishes it from preview.
        active = self.navigation.get(OWNER) if self.navigation else None
        return {mid for mid, kind in self.roles.items()
                if ("nav" if mid == active else kind) == role and mid in self.visible}


def harness(services):
    h = _Harness(services)
    h.bot.session = h.session = VisibleApi()
    h.session.navigation = h.flow.navigation_store(OWNER)
    h.flow._preview = AiogramChannelTransport(h.bot, channel_id=FLOW_CHANNEL, owner_id=OWNER)
    return h


class SizedVisibleApi(VisibleApi):
    """A media group allocates one message per item, like Telegram does;
    the base fake hardcodes two, which hides full-group cleanup budgets."""

    async def make_request(self, bot, method, timeout=None):
        if method.__api_method__ != "sendMediaGroup":
            return await super().make_request(bot, method, timeout)
        payload = method.model_dump(mode="json", exclude_none=True,
                                    exclude={"default"}, fallback=str)
        assert payload["chat_id"] == OWNER
        self.requests.append(("sendMediaGroup", payload))
        raw = [self._raw() for _ in payload["media"]]
        for item in raw:
            self.visible[item["message_id"]] = payload
            self.roles[item["message_id"]] = "preview"
        return self.check_response(bot=bot, method=method, status_code=200,
                                   content=json.dumps({"ok": True, "result": raw})).result


def sized_harness(services):
    h = _Harness(services)
    h.bot.session = h.session = SizedVisibleApi()
    h.session.navigation = h.flow.navigation_store(OWNER)
    h.flow._preview = AiogramChannelTransport(h.bot, channel_id=FLOW_CHANNEL, owner_id=OWNER)
    return h


def create_full_album(h, *, count=10):
    assert count <= 10  # Telegram's media-group cap, the worst-case transition
    h.message(text="/start")
    h.press_text(texts.MENU_NEW_POST)
    first = None
    for i in range(count):
        event = _raw_message(
            message_id=40 + i, media_group_id="album10",
            photo=[{"file_id": f"f{i}", "file_unique_id": f"u{i}",
                    "width": 100, "height": 100}],
            caption="bold" if i == 0 else None,
            caption_entities=[{"type": "bold", "offset": 0, "length": 4}] if i == 0 else None)
        h.message(**event)
        first = first or event
    h.feed({"message": first})
    h.press_text(texts.BTN_CONTINUE)
    assert len(h.session.alive("preview")) == count


def create_preview(h, *, album=False):
    h.message(text="/start")
    h.press_text(texts.MENU_NEW_POST)
    for i in range(2 if album else 1):
        event = {"photo": [{"file_id": f"dummy-{i}", "file_unique_id": f"u-{i}",
                            "width": 100, "height": 100}]}
        if album:
            event["media_group_id"] = "album"
        h.message(message_id=40+i, **event)
    h.press_text(texts.BTN_CONTINUE)


def replace_stored_preview(h):
    """A newly frozen stored snapshot is reviewed on the next card transition.

    Normal Preview buttons no longer exist. Seed an updated draft (not a UI
    edit) to exercise replacement/cleanup without leaving the current preview
    context. A manual time enters final confirmation (but never schedules).
    """
    post = h.flow._ui().arg('post_id')
    if h.flow._planned():
        from poster_bot.domain.planner import plan_post_for_version
        saved = h.flow._plans.get(post)
        signature = replace(saved.plan.signature,
                            main_text=saved.plan.signature.main_text + '!')
        plan = plan_post_for_version(saved.logical, signature,
                                     version=saved.plan.version).plan
        h.flow._plans.save_with_content(post, saved.logical, plan,
                                        plan.parts[0].content, now_epoch=h.now)
    else:
        content = h.services.drafts.get_content(post)
        if content.kind == 'text':
            content = replace(content, text=content.text + '!')
        elif content.kind == 'album':
            content = replace(content, items=(replace(
                content.items[0], caption=(content.items[0].caption or '') + '!'),
                *content.items[1:]))
        else:
            content = replace(content, media=replace(
                content.media, caption=(content.media.caption or '') + '!'))
        h.services.drafts.update(post, content, now_epoch=h.now)
    h.expected_posts = h.services.conn.execute('SELECT * FROM posts').fetchall()
    h.replacement_input = 10000 + h._update_id
    h.message(message_id=h.replacement_input, text='2359')
    # Review is presentation only, never a second business mutation.
    assert h.services.conn.execute('SELECT * FROM posts').fetchall() == h.expected_posts
    assert h.services.publications.get_by_post(post) is None


def test_leave_preview_removes_obsolete_visible_photo(services):
    h = harness(services)
    create_preview(h)
    assert len(h.session.alive("preview")) == 1
    h.press_text(texts.MENU_DRAFTS)
    assert not h.session.alive("preview")
    assert len(h.session.alive("keyboard")) == len(h.session.alive("nav")) == 1


@pytest.mark.parametrize("album", [False, True])
def test_repeated_previews_replace_all_items_after_new_bottom_controls(services, album):
    h = harness(services)
    create_preview(h, album=album)
    for _ in range(3):
        old = h.session.alive("preview")
        old_nav = h.card_id()

        def deleting(mid):
            if mid in old | {old_nav}:
                assert h.card_id() != old_nav
                assert h.card_id() in h.session.visible
                assert h.session.alive("preview") - old

        h.session.before_delete = deleting
        replace_stored_preview(h)
        h.session.before_delete = None
        current = h.session.alive("preview")
        assert len(current) == (2 if album else 1)
        assert current.isdisjoint(old)
        assert h.card_id() > max(current)
    h.press_text(texts.MENU_SETTINGS)
    assert not h.session.alive("preview")


@pytest.mark.parametrize("kind", ["text", "video", "audio", "forward"])
def test_all_output_types_cleanup_without_releasing_forward_source(services, kind):
    h = harness(services)
    h.message(text="/start")
    h.press_text(texts.MENU_NEW_POST)
    event = {"message_id": 44}
    if kind in ("text", "forward"):
        event["text"] = "saved content"
    if kind == "forward":
        event["forward_origin"] = {"type": "hidden_user", "date": h.now,
                                   "sender_user_name": "source"}
    elif kind != "text":
        event[kind] = {"file_id": "dummy", "file_unique_id": "unique", "duration": 1}
        if kind == "video":
            event[kind].update(width=100, height=100)
    h.message(**event)
    h.press_text(texts.BTN_FORWARD_AS_IS if kind == "forward" else texts.BTN_CONTINUE)
    assert h.session.alive("preview")
    saved = services.conn.execute("SELECT * FROM posts").fetchall()
    h.press_text(texts.MENU_DRAFTS)
    assert not h.session.alive("preview")
    assert services.conn.execute("SELECT * FROM posts").fetchall() == saved
    if kind == "forward":
        assert 44 in h.session.visible
        assert not any(name == "deleteMessage" and p["message_id"] == 44
                       for name, p in h.session.requests)


def test_schedule_subcards_and_back_reuse_current_frozen_preview(services):
    h = harness(services)
    create_preview(h)
    before = h.session.alive('preview')
    assert len(before) == 1
    assert texts.PICK_TIME in h.last_text()
    h.press_text(texts.BTN_DAY_SLOTS)
    assert h.session.alive('preview') == before
    assert texts.BTN_OWN_TIME not in h.last_labels()
    assert texts.BTN_CONTINUE not in h.last_labels()
    h.press_text(texts.BTN_BACK)
    assert h.session.alive('preview') == before
    h.press_text(texts.BTN_SCHEDULE)
    h.message(message_id=70, text='2359')
    assert h.last_text() == '❗ Публикация будет запланирована на 23:59 21.10.2026 ❗'
    assert h.session.alive('preview') == before
    h.press_text('Изменить дату')
    assert texts.BTN_OWN_TIME not in h.last_labels()
    assert texts.BTN_CONTINUE not in h.last_labels()
    assert h.session.alive('preview') == before
    h.press_text(texts.BTN_BACK)
    assert h.session.alive('preview') == before


def test_restart_restores_keyboard_and_cleans_persisted_preview_registry(services):
    first = harness(services)
    create_preview(first, album=True)
    api = first.session
    old = set(api.visible)
    # Recompose the real flow/router from the same disposable database.
    h = harness(services)
    h.bot.session = h.session = api
    h.flow._preview = AiogramChannelTransport(h.bot, channel_id=FLOW_CHANNEL, owner_id=OWNER)
    h.message(text="/start", message_id=55)
    assert not api.alive("preview")
    assert not (old & set(api.visible))
    assert len(api.alive("keyboard")) == len(api.alive("nav")) == 1
    h.press_text(texts.MENU_RESTART)
    assert len(api.alive("keyboard")) == len(api.alive("nav")) == 1


def test_failed_deletion_retries_later_with_active_outputs_protected(services):
    h = harness(services)
    create_preview(h)
    old_preview = next(iter(h.session.alive("preview")))
    old_nav = h.card_id()
    h.session.delete_errors.update({old_preview: "network", old_nav: "network"})
    h.press_text(texts.MENU_DRAFTS)
    assert {old_preview, old_nav} <= set(h.session.visible)
    attempted = len(h.session.requests)
    h.press_text(texts.MENU_SETTINGS)  # same clock: no immediate retry
    assert not any(n == "deleteMessage" and p["message_id"] in {old_preview, old_nav}
                   for n, p in h.session.requests[attempted:])
    h.now += 3
    h.press_text(texts.MENU_DRAFTS)
    assert not ({old_preview, old_nav} & set(h.session.visible))
    assert h.card_id() in h.session.visible


def test_retry_after_blocks_cleanup_until_next_eligible_transition(services):
    h = harness(services)
    create_preview(h)
    old_nav = h.card_id()
    h.session.delete_errors[old_nav] = "retry"
    h.press_text(texts.MENU_DRAFTS)
    start = len(h.session.requests)
    h.now += 10
    h.press_text(texts.MENU_SETTINGS)
    deletes = [p for n, p in h.session.requests[start:] if n == "deleteMessage"]
    # Only the accepted navigation input may be removed during cooldown.
    assert all(p["message_id"] >= 10000 for p in deletes)
    assert old_nav in h.session.visible
    h.now += 21
    h.press_text(texts.MENU_DRAFTS)
    assert old_nav not in h.session.visible
    assert not h.session.alive("preview")


@pytest.mark.parametrize("error", ["message to delete not found", "message can't be deleted"])
def test_terminal_cleanup_is_retired_without_retry_loop(services, error):
    h = harness(services)
    create_preview(h)
    old = h.card_id()
    h.session.delete_errors[old] = error
    h.press_text(texts.MENU_DRAFTS)
    h.now += 100
    h.press_text(texts.MENU_SETTINGS)
    assert sum(n == "deleteMessage" and p["message_id"] == old
               for n, p in h.session.requests) == 1


def test_failed_replacement_controls_preserve_working_preview_and_nav(services):
    h = harness(services)
    create_preview(h)
    old = h.session.alive("preview") | {h.card_id()}
    h.session.send_error = "temporary failure"
    with pytest.raises(TelegramBadRequest):
        replace_stored_preview(h)
    assert old <= set(h.session.visible)
    assert len(h.session.alive("preview")) == 2
    # Successful next presentation clears BOTH known groups, including the
    # candidate that survived the failed control-card send.
    h.press_text(texts.MENU_SETTINGS)
    assert not h.session.alive("preview")


def test_invalid_input_unauthorized_stale_and_refresh_do_not_remove_current_preview(services):
    h = harness(services)
    create_preview(h)
    before = h.session.alive("preview")
    start = len(h.session.requests)
    h.press_text(texts.BTN_BACK, user_id=999)
    assert h.session.requests[start:] == []
    h.feed({"callback_query": {
        "id": "stale", "from": {"id": OWNER, "is_bot": False, "first_name": "x"},
        "chat_instance": "ci", "data": "forged",
        "message": {"message_id": 888, "date": h.now,
                    "chat": {"id": OWNER, "type": "private"}},
    }})
    assert all(n == "answerCallbackQuery" for n, _ in h.session.requests[start:])
    start = len(h.session.requests)
    asyncio.run(h.dashboard.refresh(h.bot, now=h.now))
    assert not h.session.requests[start:]
    assert h.session.alive("preview") == before
    h.message(message_id=65, voice={"file_id": "v", "file_unique_id": "vu", "duration": 3})
    assert 65 not in h.session.visible
    assert h.flow.input_discarded(f'{OWNER}:65')
    assert h.session.alive('preview') == before


def test_pending_prompt_cleanup_survives_recomposition_and_protects_new_prompt(services):
    h = harness(services)
    h.message(text="/start")
    old_prompt = next(iter(h.session.alive("keyboard")))
    h.session.delete_errors[old_prompt] = "network"
    h.message(text="/start", message_id=51)
    assert len(h.session.alive("keyboard")) == 2
    api = h.session
    replacement = harness(services)
    replacement.bot.session = replacement.session = api
    replacement.now = h.now + 3
    replacement.press_text(texts.MENU_SETTINGS)
    assert old_prompt not in api.visible
    assert len(api.alive("keyboard")) == 1
    assert replacement.card_id() in api.visible


def test_unknown_history_is_not_enumerated_or_deleted(services):
    h = harness(services)
    # An old untracked bot output cannot safely be distinguished from sources.
    h.session.visible[333] = {"text": "untracked"}
    h.message(text="/start")
    h.press_text(texts.MENU_RESTART)
    assert 333 in h.session.visible
    assert not any(n == "deleteMessage" and p["message_id"] == 333
                   for n, p in h.session.requests)


def test_failed_preview_send_keeps_previous_visible_preview(services):
    h = harness(services)
    h.message(text="/start")
    h.press_text(texts.MENU_NEW_POST)
    h.message(message_id=43, text="preview text")
    h.press_text(texts.BTN_CONTINUE)
    before = h.session.alive("preview")
    old_nav = h.card_id()
    h.session.send_error = "temporary API failure"
    replace_stored_preview(h)
    assert h.session.alive("preview") == before
    assert h.card_id() != old_nav


def test_cleanup_metadata_failure_does_not_replay_business_transition(services, monkeypatch):
    h = harness(services)
    create_preview(h)
    lifecycle = h.flow._navigation.lifecycle
    original = lifecycle.presented

    def unavailable(**kwargs):
        raise RuntimeError("isolated metadata write failure")

    monkeypatch.setattr(lifecycle, "presented", unavailable)
    h.press_text(texts.MENU_DRAFTS)
    assert h.session.alive("preview")
    assert h.card_id() in h.session.visible
    monkeypatch.setattr(lifecycle, "presented", original)
    h.press_text(texts.MENU_SETTINGS)
    assert not h.session.alive("preview")


def test_full_media_group_replacement_and_leave_clear_every_item_in_transition(services):
    """A ten-item album plus its control card is one retirement generation:
    a replacement transition must clear all eleven, leave must clear the
    album completely, and neither may enumerate or loop beyond retired IDs."""
    h = sized_harness(services)
    create_full_album(h)
    post_ids = [row['id'] for row in services.conn.execute("SELECT * FROM posts")]
    for _ in range(2):
        old = h.session.alive("preview")
        old_nav = h.card_id()
        start = len(h.session.requests)
        replace_stored_preview(h)
        deletes = {p["message_id"] for n, p in h.session.requests[start:]
                   if n == "deleteMessage"}
        # Full group + card, plus only the accepted manual input itself.
        assert deletes == old | {old_nav, h.replacement_input}
        current = h.session.alive("preview")
        assert len(current) == 10 and current.isdisjoint(old)
        assert h.card_id() > max(current)
    old = h.session.alive("preview")
    old_nav = h.card_id()
    assert len(old) == 10
    start = len(h.session.requests)
    h.press_text(texts.MENU_DRAFTS)
    deletes = {p["message_id"] for n, p in h.session.requests[start:]
               if n == "deleteMessage"}
    assert old | {old_nav} <= deletes  # the album is cleared on leave too
    # Bounded work only: retired identities plus the accepted input itself.
    assert all(mid in old | {old_nav} or mid >= 10000 for mid in deletes)
    assert not h.session.alive("preview")
    assert len(h.session.visible) == 2
    assert len(h.session.alive("nav")) == len(h.session.alive("keyboard")) == 1
    assert services.conn.execute("SELECT * FROM posts").fetchall() == h.expected_posts
    assert [row['id'] for row in h.expected_posts] == post_ids
    assert not any(n.startswith(("send", "forward", "copy")) and p["chat_id"] != OWNER
                   for n, p in h.session.requests)


def test_staged_candidate_album_retires_with_next_group_in_one_transition(services):
    """Failed control-card send leaves a staged ten-item candidate visible.
    The next successful presentation retires candidate + active + card: all
    twenty-one in that single transition, still only retired identities."""
    h = sized_harness(services)
    create_full_album(h)
    active = h.session.alive("preview")
    old_nav = h.card_id()
    h.session.send_error = "temporary failure"
    with pytest.raises(TelegramBadRequest):
        replace_stored_preview(h)
    candidate = h.session.alive("preview") - active
    assert len(candidate) == 10 and len(active) == 10
    start = len(h.session.requests)
    h.press_text(texts.MENU_SETTINGS)
    deletes = {p["message_id"] for n, p in h.session.requests[start:]
               if n == "deleteMessage"}
    retired = active | candidate | {old_nav}
    assert retired <= deletes  # both complete groups clear together
    assert all(mid in retired or mid >= 10000 for mid in deletes)
    assert not h.session.alive("preview")
    # Failed control delivery must not delete the incoming message.
    assert h.replacement_input in h.session.visible
    assert len(h.session.visible) == 3


# ---- planned-stack regressions: retirement work above FRESH_CLAIM_LIMIT ----

PLANNED_SIG = SignatureConfig(main_text="НашКанал",
                              main_url="https://t.me/our_channel",
                              partner_text="Партнёр",
                              partner_url="https://t.me/partner_one")


def planned_harness(services):
    """The same dispatcher+handlers wiring (presented/cleanup run after every
    press), with the flow switched into the planned pipeline — mirroring the
    live composition. _Harness builds the legacy flow; assigning signature
    and plan store after construction is the established _preview-replacement
    style for harness-local composition changes."""
    h = harness(services)
    h.flow._signature = PLANNED_SIG
    h.flow._plans = PlanStore(services.conn)
    return h


def create_planned_stack(h) -> int:
    """Open a real stored 40-part plan through Dispatcher.

    This fixture tests retirement, not incremental intake: rebuilding every
    prefix of an 80KB body needlessly repeats whole-body planner validation.
    Creation/intake has separate Dispatcher tests; this still plans, validates,
    stores and privately renders the complete unmodified large stack.
    """
    from poster_bot.domain.planner import LogicalPost, plan_post
    paras = [("абзац " * 334)[:2000] for _ in range(40)] + ["финал"]
    logical = LogicalPost(body='\n\n'.join(paras))
    plan = plan_post(logical, PLANNED_SIG).plan
    assert plan is not None
    pid = h.flow._drafts.create_with_plan(logical, plan, now_epoch=h.now)
    h.message(text="/start")
    h.press_text(texts.MENU_DRAFTS)
    h.press_text(next(label for label in h.last_labels() if 'абзац' in label))
    stored = h.flow._plans.get(pid)
    parts = len(stored.plan.parts)
    assert parts > PreviewLifecycle.FRESH_CLAIM_LIMIT  # the >24 premise holds
    return parts


def deletes_since(h, start) -> list[int]:
    return [p["message_id"] for n, p in h.session.requests[start:]
            if n == "deleteMessage"]


def test_planned_stack_beyond_claim_limit_drains_in_one_transition(services):
    """A 40-part planned stack plus its card is ONE retirement generation:
    the replacing transition must attempt every retired id (well beyond the
    24-id claim batch — the queue refills), leave must clear the next stack
    completely, and foreign history must never be enumerated or deleted."""
    h = planned_harness(services)
    h.session.visible[333] = {"text": "foreign user message"}
    parts = create_planned_stack(h)
    stack_a = h.session.alive("preview")
    assert len(stack_a) == parts
    old_nav = h.card_id()
    start = len(h.session.requests)
    replace_stored_preview(h)
    deletes = set(deletes_since(h, start))
    assert stack_a | {old_nav} <= deletes  # 40 stack ids + card, one press
    stack_b = h.session.alive("preview")
    assert len(stack_b) == parts and stack_b.isdisjoint(stack_a)
    assert h.card_id() in h.session.visible
    assert 333 in h.session.visible  # untracked history untouched
    assert all(mid in stack_a | {old_nav} or mid >= 10000 for mid in deletes)
    start = len(h.session.requests)
    h.press_text(texts.MENU_DRAFTS)
    assert stack_b <= set(deletes_since(h, start))  # leave drains stack B too
    assert not h.session.alive("preview")


def test_failed_stack_deletes_get_one_attempt_each_per_transition(services):
    """Every retired id gets exactly one attempt in the transition that
    retired it: mass failure cannot spin an API loop, failed ids stay visible
    and pending, the same clock does not retry them, and later transitions
    drain them only from the bounded backlog budget."""
    h = planned_harness(services)
    parts = create_planned_stack(h)
    stack_a = h.session.alive("preview")
    h.session.delete_errors.update({mid: "network" for mid in stack_a})
    start = len(h.session.requests)
    replace_stored_preview(h)
    attempted = [mid for mid in deletes_since(h, start) if mid in stack_a]
    assert len(attempted) == parts  # every id tried ...
    assert len(set(attempted)) == parts  # ... exactly once, no retry loop
    assert stack_a <= set(h.session.visible)  # failures stay visible
    start = len(h.session.requests)
    h.press_text(texts.MENU_DRAFTS)  # same clock: backoff respected
    assert not [mid for mid in deletes_since(h, start) if mid in stack_a]
    cleared = 0
    for _ in range(10):  # bounded backlog drains older failures progressively
        h.now += 3
        start = len(h.session.requests)
        h.press_text(texts.MENU_SETTINGS)
        batch = {mid for mid in deletes_since(h, start) if mid in stack_a}
        assert len(batch) <= PreviewLifecycle.BACKLOG_LIMIT
        assert len(batch) == len(set(batch))
        cleared += len(batch)
        if not (stack_a & set(h.session.visible)):
            break
    assert not stack_a & set(h.session.visible)  # everything eventually removed


def test_retry_after_stops_stack_drain_and_pending_survives_cooldown(services):
    """RetryAfter mid-stack halts the pass with a durable cooldown: already
    deleted ids stay gone, un-attempted ids keep their fresh debt, nothing is
    dropped, and after the cooldown a later transition finishes the drain."""
    h = planned_harness(services)
    parts = create_planned_stack(h)
    stack_a = h.session.alive("preview")
    stopper = min(stack_a)  # pending drains in retirement order: stops early
    h.session.delete_errors[stopper] = "retry"
    start = len(h.session.requests)
    replace_stored_preview(h)
    attempted = [mid for mid in deletes_since(h, start) if mid in stack_a]
    assert attempted == [stopper]  # one hit, then the pass stops
    assert stopper in h.session.visible  # owed ids stay visible ...
    survivors = stack_a - {stopper}
    assert survivors <= set(h.session.visible)
    pending = h.flow._navigation.lifecycle._load()["pending"]
    assert all(str(mid) in pending for mid in stack_a)  # ... and stay tracked
    start = len(h.session.requests)
    h.press_text(texts.MENU_DRAFTS)  # during cooldown: no API churn
    assert not [mid for mid in deletes_since(h, start) if mid in stack_a]
    h.now += 31  # retry_after was 30
    h.press_text(texts.MENU_SETTINGS)
    assert not h.session.alive("preview")  # full drain completes afterwards


# ---- documented 48h abandonment boundary ------------------------------------
#
# preview_lifecycle.cleanup drops a pending identity WITHOUT a delete attempt
# once now - since >= 48h ("their messages are long unreachable in practice").
# 'since' is the RETIREMENT ledger time, not the real send age — cleanup
# cannot know the actual message age, so the documented policy is best-effort
# by construction. These tests pin the exact boundary of that implemented
# policy (47h59m59 attempts, exactly-48h abandons) without changing it.

H48 = 48 * 3600


def _retire_with_failed_delete(services):
    """Isolated setup: preview + card retired by leaving the preview screen;
    BOTH first delete attempts fail (network), so exactly these ids stay
    pending, demoted out of the fresh queue, with since == the fake clock."""
    h = harness(services)
    create_preview(h)
    retired = h.session.alive("preview") | {h.card_id()}
    assert len(retired) == 2
    h.session.delete_errors.update({mid: "network" for mid in retired})
    h.press_text(texts.MENU_DRAFTS)
    assert retired <= set(h.session.visible)  # both deletions failed
    pending = h.flow._navigation.lifecycle._load()["pending"]
    assert {str(mid) for mid in retired} <= set(pending)
    assert all(pending[str(mid)]["fresh"] == 0 for mid in retired)  # demoted
    return h, retired, h.now


def test_retired_output_one_second_before_48h_is_still_attempted_and_removed(services):
    """47h59m59 since retirement is BELOW the cutoff: the backlog pass must
    attempt the delete again — and succeed — not abandon the owed debt."""
    h, retired, retired_at = _retire_with_failed_delete(services)
    h.now = retired_at + H48 - 1
    start = len(h.session.requests)
    h.press_text(texts.MENU_SETTINGS)
    deletes = set(deletes_since(h, start))
    assert retired <= deletes  # both owed ids tried again in time
    assert not retired & set(h.session.visible)  # ... and removed for real
    pending = h.flow._navigation.lifecycle._load()["pending"]
    assert retired.isdisjoint({int(mid) for mid in pending})


def test_retired_output_at_exactly_48h_is_abandoned_without_any_attempt(services):
    """The cutoff tested precisely from the other side: at exactly 48h since
    retirement the ids leave the ledger WITHOUT another Telegram call — the
    total delete attempts per id stays one (only the earlier failed try), the
    messages remain visible (documented best-effort), and the current UI is
    untouched by the same transition."""
    h, retired, retired_at = _retire_with_failed_delete(services)
    h.now = retired_at + H48  # now - since == 48*3600: retire, no attempt
    start = len(h.session.requests)
    h.press_text(texts.MENU_SETTINGS)
    deletes = set(deletes_since(h, start))
    assert retired.isdisjoint(deletes)  # zero new attempts at the boundary
    assert sum(n == "deleteMessage" and p["message_id"] in retired
               for n, p in h.session.requests) == len(retired)
    assert retired <= set(h.session.visible)  # never deleted: documented
    pending = h.flow._navigation.lifecycle._load()["pending"]
    assert retired.isdisjoint({int(mid) for mid in pending})  # ledger cleared
    # current UI survives the very same transition, protected
    assert h.card_id() in h.session.visible
    keyboard = h.session.alive("keyboard")
    assert keyboard and keyboard.isdisjoint(deletes)
