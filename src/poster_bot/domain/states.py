"""Publication state machine (single source of truth for allowed moves).

Draft is not a status: a post without an active publication is a draft.
Every state change is applied as a compare-and-set (expected status + row
version), so a winner is unique under concurrency.

Meanings (GLOSSARY): scheduled = confirmed on a slot; suspended = paused
(after edit or manual pause) awaiting re-confirmation; sending = claimed,
in flight, immutable; published = done, history link lives until post
deletion; uncertain = started-but-unconfirmed outcome, owner resolves
manually; missed = slot passed unclaimed, parked until manual replanning.
"""
from __future__ import annotations

from enum import StrEnum

from .errors import TransitionError


class Status(StrEnum):
    SCHEDULED = "scheduled"
    SUSPENDED = "suspended"
    SENDING = "sending"
    PUBLISHED = "published"
    UNCERTAIN = "uncertain"
    MISSED = "missed"


#: statuses that keep their minute reserved (reused by the DB partial index)
RESERVING = frozenset(
    {Status.SCHEDULED, Status.SUSPENDED, Status.SENDING, Status.UNCERTAIN, Status.MISSED}
)

#: owner may cancel (to draft or delete) only from these
CANCELABLE = frozenset({Status.SCHEDULED, Status.SUSPENDED, Status.MISSED})

#: the overdue sweep may mark these as missed
SWEEPABLE_TO_MISSED = frozenset({Status.SCHEDULED, Status.SUSPENDED})

_ALLOWED = {
    (Status.SCHEDULED, Status.SUSPENDED),   # pause / edit-pauses
    (Status.SCHEDULED, Status.SENDING),     # claim (compare-and-set)
    (Status.SCHEDULED, Status.MISSED),      # overdue sweep
    (Status.SUSPENDED, Status.SCHEDULED),   # resume / re-confirm (future-checked)
    (Status.SUSPENDED, Status.MISSED),      # overdue sweep (slot will pass)
    (Status.SENDING, Status.PUBLISHED),     # confirmed success
    (Status.SENDING, Status.UNCERTAIN),     # recovery of unfinished attempt
    (Status.SENDING, Status.MISSED),        # definitive failure: never left the
                                            # slot, parks for manual replan
    (Status.UNCERTAIN, Status.PUBLISHED),   # owner verified in channel
    (Status.UNCERTAIN, Status.SCHEDULED),   # owner replans to a new minute
    (Status.MISSED, Status.SCHEDULED),      # manual replanning
}


def can_transition(current: Status, target: Status) -> bool:
    return (current, target) in _ALLOWED


def require_transition(current: Status, target: Status) -> None:
    if not can_transition(current, target):
        raise TransitionError(
            f"publication transition {current.value} -> {target.value} is not allowed"
        )


def is_reserving(status: Status) -> bool:
    return status in RESERVING