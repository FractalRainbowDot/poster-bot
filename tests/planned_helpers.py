"""Shared builders for planned multi-part publication tests (CHUNK4).

The planned pipeline is storage-level truth over one migrated connection:
a LogicalPost is frozen through the REAL planner (plan_post), persisted via
PlanStore (same validation path the UI uses), scheduled through the normal
publications service and then driven by SenderService wired with
plans/receipts (the gated live construction).
"""
from __future__ import annotations

import json

from conftest import NOW
from poster_bot.domain.content import Content, ForwardSource
from poster_bot.domain.planner import LogicalPost, plan_post
from poster_bot.domain.signature import SignatureConfig
from poster_bot.domain.time import Instant
from poster_bot.repo.plans import PlanStore
from poster_bot.repo.receipts import PartReceiptStore
from poster_bot.repo.sender import SenderService

# the frozen mandatory signature embedded in every planned part
SIG = SignatureConfig(
    main_text="НашКанал", main_url="https://t.me/main_channel",
    partner_text="Партнёр", partner_url="https://t.example/partner",
)
#: a DIFFERENT configured signature: ambient config changes after planning
#: must never reach the sent stack (no ambient replan)
SIG_OTHER = SignatureConfig(
    main_text="ИнойКанал", main_url="https://t.me/other_channel",
    partner_text="Другой", partner_url="https://t.example/drygoy",
)

PARA_CHARS = "abcdefghij"


def logical_body(sizes: tuple[int, ...]) -> LogicalPost:
    """N distinct blank-line-separated paragraphs of the given sizes."""
    paragraphs = [PARA_CHARS[i % len(PARA_CHARS)] * size
                  for i, size in enumerate(sizes)]
    return LogicalPost(body="\n\n".join(paragraphs))


def planned_publication(services, logical: LogicalPost, *,
                        signature: SignatureConfig = SIG,
                        epoch: int = NOW + 7200):
    """Freeze the pair through the real save path, then schedule normally.

    Returns (publication, post_id, plan)."""
    result = plan_post(logical, signature)
    assert result.plan is not None, [d.to_json() for d in result.diagnostics]
    plan = result.plan
    post_id = services.drafts.create(plan.parts[0].content, now_epoch=NOW)
    PlanStore(services.conn).save(post_id, logical, plan, now_epoch=NOW)
    pub = services.publications.schedule(post_id, Instant(epoch),
                                         now_epoch=NOW)
    return pub, post_id, plan


def gated_sender(services, transport) -> SenderService:
    """The live gated wiring: plans + receipts + editor, one conn."""
    return SenderService(
        services.dispatch,
        transport,
        plans=PlanStore(services.conn),
        receipts=PartReceiptStore(services.conn),
        editor=transport,
    )


# ---- durable-state probes (receipt truth, not call counts) -----------------
def part_rows(services, attempt_id: int) -> list[dict]:
    rows = services.conn.execute(
        "SELECT part_index, state, channel_message_ids, permalink"
        " FROM publication_parts WHERE attempt_id=? ORDER BY part_index",
        (attempt_id,),
    ).fetchall()
    return [
        {
            "index": r["part_index"],
            "state": r["state"],
            "ids": json.loads(r["channel_message_ids"])
            if r["channel_message_ids"] else None,
            "permalink": r["permalink"],
        }
        for r in rows
    ]


def nav_rows(services, attempt_id: int) -> list[dict]:
    rows = services.conn.execute(
        "SELECT p.part_index, n.state, n.last_error FROM navigation_edits n"
        " JOIN publication_parts p ON p.id = n.part_id"
        " WHERE p.attempt_id=? ORDER BY p.part_index",
        (attempt_id,),
    ).fetchall()
    return [{"index": r["part_index"], "state": r["state"],
             "last_error": r["last_error"]} for r in rows]


def pub_status(services, pub_id: int) -> str:
    return services.conn.execute(
        "SELECT status FROM publications WHERE id=?", (pub_id,)
    ).fetchone()["status"]


def latest_attempt(services, pub_id: int):
    return services.conn.execute(
        "SELECT id, state, outcome FROM send_attempts WHERE publication_id=?"
        " ORDER BY id DESC LIMIT 1",
        (pub_id,),
    ).fetchone()


def base_entities_of(part) -> tuple:
    """The entity tuple the transport originally sends for a planned part."""
    content = part.content
    if content.kind == "text":
        return content.entities
    if content.kind == "album":
        return content.caption_entities
    if content.media is not None:
        return content.media.caption_entities
    return ()


FORWARD = Content(
    kind="forward", mode="forward",
    forward=ForwardSource(chat_id=-100555, message_ids=(11, 12)),
)