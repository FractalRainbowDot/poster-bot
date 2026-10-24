"""Draft writer CAS and fresh cleanup scans, using isolated SQLite only."""
import time

import pytest

from conftest import NOW
from poster_bot.db import connect
from poster_bot.domain.content import Content
from poster_bot.domain.errors import ContentValidationError, TransitionError
from poster_bot.domain.planner import LogicalPost, plan_post
from poster_bot.domain.original_forward import plan_primary_content
from poster_bot.repo.plans import PlanStore
from test_repo_original_forward import pair, rows
from test_repo_plans import SIG


@pytest.mark.parametrize('native', [False, True])
def test_draft_guard_rejects_second_connection_change_inside_transaction(services, native):
    logical, plan = pair() if native else signed('original')
    post = services.drafts.create_with_plan(logical, plan, now_epoch=NOW)
    store = PlanStore(services.conn)
    expected = store.content_snapshot(post)
    replacement, replacement_plan = signed('replacement')
    path = services.conn.execute('PRAGMA database_list').fetchone()['file']
    other = connect(path)
    # Simulate the race AFTER the UI preflight, immediately BEFORE BEGIN.
    from poster_bot.repo import plans
    original_tx = plans.immediate_tx
    from contextlib import contextmanager

    @contextmanager
    def interleaved(conn):
        other.execute('UPDATE posts SET content_json=? WHERE id=?',
                      (Content(kind='text', text='external edit').to_json(), post))
        with original_tx(conn) as tx:
            yield tx

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(plans, 'immediate_tx', interleaved)
        with pytest.raises((TransitionError, ContentValidationError)):
            store.save_with_content(post, replacement, replacement_plan,
                plan_primary_content(replacement_plan), expected_snapshot=expected)
    stable = rows(services.conn)
    assert services.drafts.get_content(post).text == 'external edit'
    assert stable['post_plans'][0][1:3] == (logical.to_json(), plan.to_json())
    assert stable['publications'] == stable['journal_events'] == []
    other.close()


def signed(body):
    logical = LogicalPost(body=body)
    return logical, plan_post(logical, SIG).plan


def test_matching_guard_preserves_identity_and_ignores_ui_versions(services):
    logical, plan = pair()
    post = services.drafts.create_with_plan(logical, plan, now_epoch=NOW)
    store = PlanStore(services.conn)
    expected = store.content_snapshot(post)
    created = store.get(post).created_at
    # Unrelated metadata is not an edit to the reviewed content.
    services.conn.execute('UPDATE posts SET updated_at=? WHERE id=?', ('later', post))
    new, new_plan = signed('copy')
    store.save_with_content(post, new, new_plan, plan_primary_content(new_plan),
                            expected_snapshot=expected, now_epoch=NOW + 1)
    assert store.get(post).created_at == created
    assert services.conn.execute('SELECT created_at FROM posts WHERE id=?', (post,)).fetchone()[0] == created


@pytest.mark.parametrize('mutation', ['logical', 'plan', 'absence'])
def test_guard_binds_both_pair_members_and_explicit_absence(services, mutation):
    logical, plan = signed('original')
    post = services.drafts.create_with_plan(logical, plan)
    store = PlanStore(services.conn)
    expected = store.content_snapshot(post)
    new, new_plan = signed('changed')
    if mutation == 'absence':
        services.conn.execute('DELETE FROM post_plans WHERE post_id=?', (post,))
    elif mutation == 'logical':
        services.conn.execute('UPDATE post_plans SET logical_json=? WHERE post_id=?',
                              (new.to_json(), post))
    else:
        services.conn.execute('UPDATE post_plans SET plan_json=? WHERE post_id=?',
                              (new_plan.to_json(), post))
    before = rows(services.conn)
    with pytest.raises(TransitionError):
        store.save_with_content(post, new, new_plan, plan_primary_content(new_plan),
                                expected_snapshot=expected)
    assert rows(services.conn) == before


def test_guarded_legacy_absence_and_write_failure_rollback(services):
    post = services.drafts.create(Content(kind='text', text='unsigned legacy'))
    store = PlanStore(services.conn)
    expected = store.content_snapshot(post)
    new, new_plan = pair()
    services.conn.execute("CREATE TEMP TRIGGER reject_guarded BEFORE INSERT ON post_plans "
                          "BEGIN SELECT RAISE(ABORT, 'test'); END")
    before = rows(services.conn)
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        store.save_with_content(post, new, new_plan, plan_primary_content(new_plan),
                                expected_snapshot=expected)
    assert rows(services.conn) == before
    services.conn.execute('DROP TRIGGER reject_guarded')
    store.save_with_content(post, new, new_plan, plan_primary_content(new_plan),
                            expected_snapshot=expected)
    assert store.get(post).plan == new_plan


def test_source_cache_bounds_and_no_exception_caching(services):
    store = PlanStore(services.conn)
    for i in range(140):
        store._cached_sources(('test', str(i)), frozenset)
    assert len(store._source_cache) == 128
    assert store._source_cache_bytes < 4 * 1024 * 1024
    huge = ('test', 'x' * (1024 * 1024 + 1))
    store._cached_sources(huge, frozenset)
    assert huge not in store._source_cache
    def invalid():
        raise ContentValidationError('test')
    with pytest.raises(ContentValidationError):
        store._cached_sources(('bad',), invalid)
    assert ('bad',) not in store._source_cache


@pytest.mark.parametrize('native', [False, True])
def test_dispatcher_confirmation_passes_original_snapshot_to_atomic_writer(services, monkeypatch, native):
    from test_ui_edit_modes import editing, MODES, labels
    from test_ui_live_preview import live_harness
    from test_ui_handlers_offline import OWNER
    from poster_bot.domain.original_forward import (OriginalForwardInput,
        OriginalForwardSource, build_original_forward_plan)
    from poster_bot.ui import texts
    if native:
        h = live_harness(services)
        logical = OriginalForwardInput(OriginalForwardSource(OWNER, (30,)))
        plan = build_original_forward_plan(logical, owner_user_id=OWNER,
                                           channel_id=h.flow._channel_id)
        post = services.drafts.create_with_plan(logical, plan, now_epoch=NOW)
        h.press_text(texts.MENU_DRAFTS)
        h.press_text(labels(h)[0])
        h.press_text(texts.BTN_EDIT_CONTENT)
        h.press_text('Заменить копией с подписью')
    else:
        h, post, _ = editing(services)
        h.press_text(MODES[2])
    h.message(message_id=50, text='candidate')
    h.press_text(texts.BTN_CONTINUE)
    assert texts.BTN_CONFIRM in labels(h)
    path = services.conn.execute('PRAGMA database_list').fetchone()['file']
    other = connect(path)
    original_write = h.flow._plans.save_with_content
    external, external_plan = signed('concurrent winner')
    captured = []

    def racing_write(*args, **kwargs):
        assert kwargs['expected_snapshot'] == h.flow._ui().arg('edit_snapshot')
        PlanStore(other).save_with_content(post, external, external_plan,
                                           plan_primary_content(external_plan))
        captured.append(rows(other))
        return original_write(*args, **kwargs)

    monkeypatch.setattr(h.flow._plans, 'save_with_content', racing_write)
    h.press_text(texts.BTN_CONFIRM)
    assert captured and rows(services.conn) == captured[0]
    assert h.flow._ui().arg('staged')  # replacement remains recoverable
    other.close()


def test_cached_scan_observes_external_native_insertion_corruption_and_removal(services):
    store = PlanStore(services.conn)
    assert not store.protected_original_forward_coordinates()
    path = services.conn.execute('PRAGMA database_list').fetchone()['file']
    other = connect(path)
    from poster_bot.repo.drafts import DraftStore
    logical, plan = pair()
    post = DraftStore(other).create_with_plan(logical, plan)
    expected = frozenset({(42, 10), (42, 11)})
    assert store.protected_original_forward_coordinates() == expected
    other.execute("UPDATE post_plans SET logical_json='{}' WHERE post_id=?", (post,))
    with pytest.raises(ContentValidationError):
        store.protected_original_forward_coordinates()
    other.execute('UPDATE post_plans SET logical_json=? WHERE post_id=?',
                  (logical.to_json(), post))
    assert store.protected_original_forward_coordinates() == expected
    DraftStore(other).delete(post)
    assert not store.protected_original_forward_coordinates()
    other.close()


def test_cleanup_repeated_large_signed_scan_parses_once_and_sees_mutation(services, monkeypatch):
    from poster_bot.repo import plans
    logical, plan = signed('\n\n'.join(['paragraph ' * 220] * 40))
    assert len(plan.parts) >= 20
    post = services.drafts.create_with_plan(logical, plan)
    store = PlanStore(services.conn)
    real_parse = plans.parse_publication_plan
    calls = []

    def counted(raw):
        calls.append(1)
        return real_parse(raw)

    monkeypatch.setattr(plans, 'parse_publication_plan', counted)
    started = time.perf_counter()
    for _ in range(4):
        assert store.protected_original_forward_coordinates() == frozenset()
    print(f'large scan seconds={time.perf_counter()-started:.3f}, parses={len(calls)}')
    assert len(calls) == 1
    native, native_plan = pair()
    other_post = services.drafts.create_with_plan(native, native_plan)
    assert store.protected_original_forward_coordinates() == frozenset({(42, 10), (42, 11)})
    services.drafts.delete(other_post)
    assert store.protected_original_forward_coordinates() == frozenset()
    services.conn.execute("UPDATE post_plans SET plan_json='{broken' WHERE post_id=?", (post,))
    with pytest.raises(ContentValidationError):
        store.protected_original_forward_coordinates()
