"""Durable bot-output identities, never source/content or channel identities.

All callers serialize with the UI presentation lock. A successful preview is
staged immediately; only a successfully published control card retires the
previous visible group. Cleanup runs on user transitions only and is bounded:
identities retired by the current transition drain completely within it (a
replacement retires a full media group plus its card), while older failed
deletions drain only from a capped backlog budget.
"""
from __future__ import annotations

import json

from ..db import iso
from ..repo.publications import immediate_tx


class PreviewLifecycle:
    # FRESH_CLAIM_LIMIT sizes ONE claim batch of the in-memory fresh queue,
    # not the deletions a transition may perform: whenever that queue drains,
    # still-untried fresh ids are re-claimed, so a transition removes every
    # identity it retired itself (measured: a 40-part planned stack plus card
    # drains completely — /tmp/opencode/probe_preview_cap2.py). The real
    # per-transition budget is one attempt per identity (an entry demotes
    # before its delete) plus the RetryAfter early stop. BACKLOG_LIMIT bounds
    # the single opportunistic pass over older failed ids only.
    FRESH_CLAIM_LIMIT = 24
    BACKLOG_LIMIT = 8

    def __init__(self, conn, owner_id: int):
        self.conn = conn
        self.owner_id = owner_id
        self.key = f"owner_outputs:v1:{owner_id}"

    def _load(self):
        row = self.conn.execute("SELECT value FROM ui_state WHERE key=?", (self.key,)).fetchone()
        return json.loads(row["value"]) if row else {
            "version": 1, "active": [], "candidate": [], "pending": {}, "retry_at": 0,
        }

    def _save(self, data, *, now):
        self.conn.execute(
            "INSERT INTO ui_state(key,value,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (self.key, json.dumps(data), iso(now)),
        )

    @staticmethod
    def _retire(data, ids, now):
        for mid in ids:
            # "fresh" marks a just-retired group member: its complete group is
            # owed a deletion attempt within the transition that retired it.
            data["pending"].setdefault(
                str(mid), {"since": now, "due": now, "attempts": 0, "fresh": 1})

    def replace_card(self, old, new, *, now):
        """Called INSIDE NavigationStore's atomic card identity transaction."""
        data = self._load()
        if old is not None and old != new:
            self._retire(data, [old], now)
        data["pending"].pop(str(new), None)
        self._save(data, now=now)

    def stage(self, ids, *, now, complete=True):
        # Only trusted Sent.message_ids from the configured private transport.
        ids = list(dict.fromkeys(mid for mid in ids
                                 if type(mid) is int and mid > 0))
        with immediate_tx(self.conn):
            data = self._load()
            # A prior successful send whose controls failed stays known too.
            self._retire(data, data["candidate"], now)
            data["candidate"] = ids
            data['candidate_complete'] = complete
            data.pop("candidate_key", None)
            data.pop('candidate_snapshot', None)
            data.pop('candidate_review', None)
            for mid in ids:
                data["pending"].pop(str(mid), None)
            self._save(data, now=now)

    def matches(self, key):
        data = self._load()
        return bool(data['active']) and data.get('active_key') == key

    def rebind_active(self, old_key, new_key, *, now, snapshot=None):
        """A staged snapshot became the identical durable stored plan."""
        with immediate_tx(self.conn):
            data = self._load()
            if (data['active'] and data.get('active_key') == old_key
                    and (snapshot is None or data.get('active_snapshot') == snapshot)):
                data['active_key'] = new_key
                self._save(data, now=now)

    def candidate_matches(self, key):
        data = self._load()
        return (bool(data['candidate']) and data.get('candidate_complete', True)
                and data.get('candidate_key') == key)

    def review_failed(self, key):
        return self._load().get('review_failed') == key

    def set_review_failure(self, key, *, now):
        with immediate_tx(self.conn):
            data = self._load()
            data['review_failed'] = key
            self._save(data, now=now)

    def complete_candidate(self, *, now):
        with immediate_tx(self.conn):
            data = self._load()
            data['candidate_complete'] = True
            self._save(data, now=now)

    def approved_review(self, key):
        """Only centrally promoted, fully presented candidates authorize edits."""
        data = self._load()
        if data['active'] and data.get('active_key') == key:
            return data.get('active_review')
        return None

    def prepared_review(self, key):
        data = self._load()
        if data.get('prepared_review_key') == key:
            return data.get('prepared_review')
        return None

    def prepare_review(self, key, review, *, now):
        """Freeze an attempted candidate; preparation is never approval."""
        with immediate_tx(self.conn):
            data = self._load()
            data['prepared_review_key'] = key
            data['prepared_review'] = review
            self._save(data, now=now)

    def bind_candidate(self, key, *, now, snapshot=None, review=None):
        with immediate_tx(self.conn):
            data = self._load()
            if data['candidate']:
                data['candidate_key'] = key
                data['candidate_snapshot'] = snapshot
                data['candidate_review'] = review
            self._save(data, now=now)

    def presented(self, *, keep_preview, now):
        with immediate_tx(self.conn):
            data = self._load()
            if keep_preview:
                if data["candidate"] and data.get('candidate_complete', True):
                    self._retire(data, data["active"], now)
                    data["active"] = data["candidate"]
                    data['active_key'] = data.pop('candidate_key', None)
                    data['active_snapshot'] = data.pop('candidate_snapshot', None)
                    data['active_review'] = data.pop('candidate_review', None)
                    data["candidate"] = []
            else:
                self._retire(data, data["active"] + data["candidate"], now)
                data["active"] = data["candidate"] = []
                data.pop('active_key', None)
                data.pop('candidate_key', None)
                data.pop('active_snapshot', None)
                data.pop('candidate_snapshot', None)
                data.pop('active_review', None)
                data.pop('candidate_review', None)
                data.pop('prepared_review_key', None)
                data.pop('prepared_review', None)
            for mid in data["active"]:
                data["pending"].pop(str(mid), None)
            self._save(data, now=now)

    def failed_candidate(self, *, now):
        """Incomplete stacks are tracked for cleanup, never promoted as complete."""
        with immediate_tx(self.conn):
            data = self._load()
            self._retire(data, data['candidate'], now)
            data['candidate'] = []
            data.pop('candidate_key', None)
            data.pop('candidate_snapshot', None)
            data.pop('candidate_review', None)
            self._save(data, now=now)

    async def cleanup(self, bot, chat_id, *, now, protected, source_referenced=None):
        from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

        if chat_id != self.owner_id:
            return
        # No sleeps, background worker or retries inside a transition. Every
        # freshly-retired due identity gets exactly one deletion attempt in
        # the transition that retired it, so a replacement clears its whole
        # album plus card. Older failures drain only from the capped backlog
        # budget; a claimed entry demotes on its first attempt, so persistent
        # deletion failures can never turn the fresh pass into an API loop.
        # RetryAfter still stops the transition early with a cooldown.
        fresh: list[str] = []
        backlog_left = self.BACKLOG_LIMIT
        while True:
            data = self._load()
            if now < data["retry_at"]:
                return
            active = set(protected) | set(data["active"]) | set(data["candidate"])
            if not fresh:
                fresh = [mid for mid, item in data["pending"].items()
                         if item.get("fresh") and int(mid) not in active
                         and item["due"] <= now][:self.FRESH_CLAIM_LIMIT]
            entry = None
            if fresh:
                mid = fresh.pop(0)
                item = data["pending"].get(mid)
                if item is not None and item["due"] <= now and int(mid) not in active:
                    entry = (mid, item)
                else:
                    continue  # became current or was resolved meanwhile
            elif backlog_left:
                entry = next(((mid, item) for mid, item in data["pending"].items()
                              if not item.get("fresh") and int(mid) not in active
                              and item["due"] <= now), None)
                if entry is None:
                    return
                backlog_left -= 1
            else:
                return
            mid, item = entry
            # entries older than 48h are abandoned without an attempt: their
            # messages are long unreachable in practice, and retrying them
            # forever would spend the owner's delete budget on dead ids.
            retire = now - item["since"] >= 48 * 3600
            stop = False
            if not retire:
                item["fresh"] = 0
                # Recheck executable source references immediately before deletion.
                # Failure to read any reference means deletion is not safe.
                if source_referenced is not None:
                    try:
                        needed = source_referenced(chat_id=chat_id, message_id=int(mid))
                    except Exception:
                        needed = True
                    if needed:
                        item['due'] = now + 300
                        with immediate_tx(self.conn):
                            self._save(data, now=now)
                        continue
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=int(mid),
                                             request_timeout=1)
                    retire = True
                except TelegramRetryAfter as exc:
                    data["retry_at"] = now + max(1, exc.retry_after)
                    item["due"] = data["retry_at"]
                    stop = True
                except TelegramForbiddenError:
                    retire = True
                except TelegramBadRequest as exc:
                    reason = exc.message.lower()
                    retire = any(text in reason for text in (
                        "message to delete not found", "message can't be deleted",
                        "message cannot be deleted", "message_id_invalid",
                    ))
                except Exception:
                    pass  # no content or raw Telegram errors in diagnostics
            if retire:
                data["pending"].pop(mid, None)
            elif not stop:
                item["attempts"] += 1
                item["due"] = now + min(300, 2 ** min(item["attempts"], 8))
            with immediate_tx(self.conn):
                self._save(data, now=now)
            if stop:
                return
