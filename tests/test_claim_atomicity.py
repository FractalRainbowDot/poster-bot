"""Concurrency acceptance (Phase 4 preview): compare-and-set claim against
parallel editing/claiming on REAL SQLite files — exactly one winner — plus
DB-level minute uniqueness under a schedule race.

sqlite3 connections are thread-affine: each worker builds its own connection
inside its own thread, then all threads hit one barrier and race.
"""
from __future__ import annotations

import threading

from conftest import NOW, future, text_content
from poster_bot.db import connect, migrate
from poster_bot.domain.errors import MinuteTakenError, TransitionError
from poster_bot.domain.states import Status
from poster_bot.journal import Journal
from poster_bot.repo.dispatch import DispatchService
from poster_bot.repo.drafts import DraftStore
from poster_bot.repo.publications import PublicationsService


class Worker:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self.conn = None
        self.pubs = None
        self.dispatch = None
        self.result: object = None
        self.error: BaseException | None = None

    def setup(self) -> None:
        self.conn = connect(self._db_path)
        journal = Journal(self.conn)
        self.pubs = PublicationsService(self.conn, journal)
        self.dispatch = DispatchService(self.conn, journal)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()


def _race(workers: list[Worker], actions) -> None:
    """actions[i] is a zero-arg callable using workers[i] services."""
    barrier = threading.Barrier(len(workers))
    threads = []

    for worker, action in zip(workers, actions):

        def target(worker=worker, action=action):
            worker.setup()  # thread-affine connection inside the thread
            barrier.wait()
            try:
                worker.result = action()
            except BaseException as exc:  # surfaced in assertions
                worker.error = exc
            finally:
                worker.close()  # close in the owning thread too

        threads.append(threading.Thread(target=target))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)


def _seed(tmp_path, name: str):
    db_path = str(tmp_path / name)
    seed = connect(db_path)
    migrate(seed)
    journal = Journal(seed)
    seed.close()
    return db_path, journal


def test_parallel_claims_exactly_one_winner(tmp_path):
    db_path, _ = _seed(tmp_path, "race.sqlite3")
    seed = connect(db_path)
    post_id = DraftStore(seed).create(text_content(), now_epoch=NOW)
    pub = PublicationsService(seed, Journal(seed)).schedule(
        post_id, future(), now_epoch=NOW
    )
    seed.close()

    workers = [Worker(db_path) for _ in range(8)]
    actions = [
        (lambda w=w, p=pub: w.dispatch.claim(p.id, p.version, now_epoch=p.scheduled_epoch))
        for w in workers
    ]
    _race(workers, actions)

    winners = [w for w in workers if w.error is None]
    losers = [w for w in workers if w.error is not None]
    assert len(winners) == 1, [w.error for w in workers]
    assert all(isinstance(w.error, TransitionError) for w in losers)
    check = connect(db_path)
    pub_now = PublicationsService(check, Journal(check)).get(pub.id)
    assert pub_now.status is Status.SENDING
    attempts = check.execute(
        "SELECT COUNT(*) c FROM send_attempts WHERE publication_id=?", (pub.id,)
    ).fetchone()
    assert attempts["c"] == 1  # exactly one persistent pre-call attempt
    check.close()


def test_claim_versus_edit_exactly_one_winner(tmp_path):
    db_path, _ = _seed(tmp_path, "race2.sqlite3")
    seed = connect(db_path)
    post_id = DraftStore(seed).create(text_content("original"), now_epoch=NOW)
    pub = PublicationsService(seed, Journal(seed)).schedule(
        post_id, future(), now_epoch=NOW
    )
    seed.close()

    claimer, editor = Worker(db_path), Worker(db_path)
    actions = [
        lambda: claimer.dispatch.claim(
            pub.id, pub.version, now_epoch=pub.scheduled_epoch
        ),
        lambda: editor.pubs.edit_content(
            pub.id, pub.version, text_content("late edit"), now_epoch=NOW
        ),
    ]
    _race([claimer, editor], actions)

    won = [w for w in (claimer, editor) if w.error is None]
    lost = [w for w in (claimer, editor) if w.error is not None]
    assert len(won) == 1 and len(lost) == 1
    assert isinstance(lost[0].error, TransitionError)

    check = connect(db_path)
    pub_now = PublicationsService(check, Journal(check)).get(pub.id)
    text_after = DraftStore(check).get_content(post_id).text
    if won[0] is claimer:
        assert pub_now.status is Status.SENDING
        assert text_after == "original"  # losing edit left nothing behind
    else:
        assert pub_now.status is Status.SUSPENDED
        assert text_after == "late edit"
        assert (
            check.execute(
                "SELECT COUNT(*) c FROM send_attempts WHERE publication_id=?",
                (pub.id,),
            ).fetchone()["c"]
            == 0
        )
    check.close()


def test_parallel_schedule_same_minute_unique(tmp_path):
    db_path, _ = _seed(tmp_path, "race3.sqlite3")
    seed = connect(db_path)
    drafts_seed = DraftStore(seed)
    post_ids = [
        drafts_seed.create(text_content(f"post {i}"), now_epoch=NOW)
        for i in range(6)
    ]
    seed.close()

    workers = [Worker(db_path) for _ in post_ids]
    actions = [
        (lambda w=w, p=p: w.pubs.schedule(p, future(), now_epoch=NOW))
        for w, p in zip(workers, post_ids)
    ]
    _race(workers, actions)

    winners = [w for w in workers if w.error is None]
    losers = [w for w in workers if w.error is not None]
    assert len(winners) == 1
    assert all(isinstance(w.error, MinuteTakenError) for w in losers)
    check = connect(db_path)
    minutes = check.execute(
        "SELECT COUNT(DISTINCT minute_epoch) c FROM publications"
    ).fetchone()
    assert minutes["c"] == 1  # real SQLite uniqueness held
    check.close()