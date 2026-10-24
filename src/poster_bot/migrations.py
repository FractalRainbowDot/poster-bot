"""Numbered SQL migrations.

Each migration is (version, name, tuple-of-statements). Statements run inside
one transaction together with the schema_migrations record.

Design notes:
- publications.status is the single source of truth for the state machine;
  'draft' is not a status: a post without an active publication IS a draft.
- uq_reserved_minute: a UTC minute stays reserved while a publication is in
  any of scheduled/suspended/sending/uncertain/missed. 'published' releases
  it (the slot is consumed and history links live separately); cancel and
  delete free it too. Keeping 'uncertain' and 'missed' reserved is the safe
  choice: an uncertain send may have actually hit the channel, so the minute
  must not be reused until the owner resolves it; 'missed' stays parked on
  its slot until manual replanning per PLAN §2.
"""
from __future__ import annotations

# Statuses whose publications reserve their minute (and are 'active').
RESERVING_STATUSES = ("scheduled", "suspended", "sending", "uncertain", "missed")

_V1 = (
    """CREATE TABLE posts (
           id INTEGER PRIMARY KEY,
           content_json TEXT NOT NULL,
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL
       )""",
    """CREATE TABLE publications (
           id INTEGER PRIMARY KEY,
           post_id INTEGER NOT NULL UNIQUE REFERENCES posts(id) ON DELETE CASCADE,
           status TEXT NOT NULL CHECK (status IN
               ('scheduled','suspended','sending','published','uncertain','missed')),
           minute_epoch INTEGER NOT NULL,
           scheduled_epoch INTEGER NOT NULL,
           scheduled_at TEXT NOT NULL,
           mode TEXT NOT NULL CHECK (mode IN ('forward','copy')),
           version INTEGER NOT NULL DEFAULT 1,
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL,
           confirmed_at TEXT
       )""",
    """CREATE UNIQUE INDEX uq_reserved_minute ON publications(minute_epoch)
       WHERE status IN ('scheduled','suspended','sending','uncertain','missed')""",
    """CREATE TABLE send_attempts (
           id INTEGER PRIMARY KEY,
           publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
           state TEXT NOT NULL CHECK (state IN ('prepared','in_progress','finished','abandoned')),
           outcome TEXT CHECK (outcome IN ('published','definitive_failure')),
           created_at TEXT NOT NULL,
           finished_at TEXT
       )""",
    """CREATE TABLE published_history (
           id INTEGER PRIMARY KEY,
           publication_id INTEGER NOT NULL UNIQUE REFERENCES publications(id) ON DELETE CASCADE,
           post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
           channel_message_ids TEXT NOT NULL,
           permalink TEXT NOT NULL,
           published_at TEXT NOT NULL
       )""",
    """CREATE TABLE settings (
           key TEXT PRIMARY KEY,
           value TEXT NOT NULL,
           updated_at TEXT NOT NULL
       )""",
    """CREATE TABLE manual_times (
           hh INTEGER NOT NULL CHECK (hh BETWEEN 0 AND 23),
           mm INTEGER NOT NULL CHECK (mm BETWEEN 0 AND 59),
           last_used_at TEXT NOT NULL,
           PRIMARY KEY (hh, mm)
       )""",
    """CREATE TABLE journal_events (
           id INTEGER PRIMARY KEY,
           ts_epoch INTEGER NOT NULL,
           ts TEXT NOT NULL,
           kind TEXT NOT NULL,
           post_id INTEGER,
           publication_id INTEGER,
           detail TEXT NOT NULL
       )""",
    "CREATE INDEX ix_journal_ts_epoch ON journal_events(ts_epoch)",
)

_V2 = (
    # key/value runtime facts (heartbeat epochs for outage detection and
    # recovery notices); values written by code, never free-form user data.
    """CREATE TABLE runtime_state (
           key TEXT PRIMARY KEY,
           value TEXT NOT NULL,
           updated_at TEXT NOT NULL
       )""",
    # durable owner-notice outbox: recovery notices are persisted at startup
    # so a crash can never lose one; delivered at-least-once, marked sent.
    # UNIQUE(kind, downtime_start_epoch) dedups the same outage across restarts.
    """CREATE TABLE owner_notices (
           id INTEGER PRIMARY KEY,
           kind TEXT NOT NULL CHECK (kind IN ('recovery')),
           downtime_start_epoch INTEGER NOT NULL,
           downtime_end_epoch INTEGER NOT NULL,
           affected_ids TEXT NOT NULL CHECK (affected_ids LIKE '[%'),
           status TEXT NOT NULL CHECK (status IN ('pending','sent')),
           created_at TEXT NOT NULL,
           sent_at TEXT,
           UNIQUE (kind, downtime_start_epoch)
       )""",
    # persistent update dedup for the polling runtime (PLAN §6 decision):
    # processed update ids survive restarts; backlog never re-replayed.
    """CREATE TABLE processed_updates (
           update_id INTEGER PRIMARY KEY,
           processed_at TEXT NOT NULL
       )""",
)

_V3 = (
    # Durable owner-conversation state (phase 2 UI): one-owner bot keeps one
    # conversation row. The version column is the compare-and-set token that
    # makes stale steps/replies lose; collection state survives restarts.
    """CREATE TABLE ui_state (
           key TEXT PRIMARY KEY,
           value TEXT NOT NULL,
           updated_at TEXT NOT NULL
       )""",
    # Server-issued action tokens for keyboards: callback data carries only
    # the token; status/version/minute re-validation happens server-side at
    # execution, so forged or stale callbacks can never mutate state.
    # 'invalidated' marks tokens superseded by newer UI steps.
    """CREATE TABLE owner_actions (
           token TEXT PRIMARY KEY,
           action TEXT NOT NULL,
           post_id INTEGER,
           publication_id INTEGER,
           expected_version INTEGER,
           minute_epoch INTEGER,
           arg TEXT,
           status TEXT NOT NULL CHECK (status IN ('pending','consumed','invalidated')),
           created_at TEXT NOT NULL
       )""",
    # Durable inbox (PLAN §6 pending-updates decision): the receipt is written
    # before handling and completion only after the update's effects are
    # persisted, so a crash re-delivers the update and the pending row is
    # reprocessed idempotently; nothing is silently discarded.
    """CREATE TABLE inbox_updates (
           update_id INTEGER PRIMARY KEY,
           received_at TEXT NOT NULL,
           done_at TEXT
       )""",
)

_V4 = (
    # Durable per-effect keys for owner-message intake (PLAN §6 idempotency
    # decision): the handler is at-least-once (begin -> handler -> complete),
    # so a crash between the flow's committed effect and inbox.complete makes
    # Telegram redeliver the same message. The key is inserted atomically
    # with the state write it guards; a replay sees the key already present
    # and the flow answers idempotently instead of applying the effect twice.
    """CREATE TABLE ui_effects (
           effect_key TEXT PRIMARY KEY,
           applied_at TEXT NOT NULL
       )""",
)

_V5 = (
    # CHUNK2: the frozen logical post + publication plan snapshot, keyed by
    # post. One row per post; replaced atomically while the composition is
    # still editable (draft/scheduled/suspended/missed) and frozen from the
    # moment the publication is claimed. The row NEVER lives in posts.
    # content_json or ui_state: both are other contracts (single physical
    # Content compatibility and volatile UI conversation state). The plan
    # embeds its own validated SignatureConfig, so a later .env change can
    # never mutate an already planned publication. JSON validity and plan
    # semantics are re-validated strictly on every read (repo/plans.py).
    """CREATE TABLE post_plans (
           post_id INTEGER PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
           logical_json TEXT NOT NULL CHECK (logical_json LIKE '{%'),
           plan_json TEXT NOT NULL CHECK (plan_json LIKE '{%'),
           plan_version INTEGER NOT NULL CHECK (plan_version > 0),
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL
       )""",
    # Per-attempt part send intents, written in the SAME transaction as the
    # claim CAS (durable intent BEFORE any channel call) and turned into
    # receipts immediately after each transport ack. A crash between intent
    # and receipt leaves 'pending' rows: the publication resolves as
    # 'uncertain' via the existing recovery sweep and is never auto-retried,
    # so a pending row next to a resolved attempt is durable evidence of the
    # exact part whose outcome is unknown. A multi-part publication whose
    # parts all carry receipts is completed through dispatch.complete with
    # the union of the parts' ids and one part's canonical permalink — the
    # published_history contract stays exactly the old one (compat read).
    # Single-part legacy publications never get rows here.
    """CREATE TABLE publication_parts (
           id INTEGER PRIMARY KEY,
           attempt_id INTEGER NOT NULL REFERENCES send_attempts(id) ON DELETE CASCADE,
           part_index INTEGER NOT NULL CHECK (part_index >= 1),
           part_count INTEGER NOT NULL CHECK (part_count >= 1),
           state TEXT NOT NULL CHECK (state IN ('pending','confirmed','uncertain','failed')),
           part_snapshot_json TEXT NOT NULL CHECK (part_snapshot_json LIKE '{%'),
           channel_message_ids TEXT CHECK (channel_message_ids IS NULL OR channel_message_ids LIKE '[%'),
           permalink TEXT,
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL,
           confirmed_at TEXT,
           UNIQUE (attempt_id, part_index),
           CHECK (part_index <= part_count),
           CHECK (state <> 'confirmed'
                  OR (permalink IS NOT NULL AND channel_message_ids IS NOT NULL))
       )""",
    # Navigation edits are NOT send attempts: they are separate durable
    # attempts to editMessageText/caption of ALREADY RECEIVED parts, allowed
    # only once every part of the attempt has a receipt and the publication
    # completed. A failed edit never touches the part receipt rows (the
    # published bodies are immutable), never re-sends, never re-claims.
    # Target URLs are built by the sender from the configured channel and
    # the stored receipts — never from callback data.
    """CREATE TABLE navigation_edits (
           id INTEGER PRIMARY KEY,
           part_id INTEGER NOT NULL UNIQUE REFERENCES publication_parts(id) ON DELETE CASCADE,
           state TEXT NOT NULL CHECK (state IN ('pending','confirmed','failed')),
           last_error TEXT,
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL
       )""",
)

MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (1, "baseline", _V1),
    (2, "runtime_outbox", _V2),
    (3, "ui_durable", _V3),
    (4, "ui_effects", _V4),
    (5, "composite_publication", _V5),
)

CURRENT_VERSION = MIGRATIONS[-1][0]