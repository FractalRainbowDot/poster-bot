"""Domain errors. Messages are owner-presentable explanations."""
from __future__ import annotations


class DomainError(ValueError):
    """Base for domain validation failures."""


class ContentValidationError(DomainError):
    """Post content does not fit Telegram limits; rejected, never truncated."""


class LocalTimeError(DomainError):
    """Owner-entered wall-clock time is not usable."""


class NonLocalTimeError(LocalTimeError):
    """The wall-clock time does not exist that day (DST spring-forward gap)."""


class AmbiguousLocalTimeError(LocalTimeError):
    """The wall-clock time occurs twice that day (DST fall-back fold)."""


class PastTimeError(DomainError):
    """Publication time must be strictly in the future at confirmation."""


class MinuteTakenError(DomainError):
    """The chosen UTC minute is already reserved by another publication."""


class NoSlotAvailableError(DomainError):
    """No free future slot exists inside the search horizon."""


class ConflictError(DomainError):
    """The post already has a publication row (one active per post)."""


class PublicationNotFound(DomainError):
    pass


class AttemptNotFound(DomainError):
    pass


class TransitionError(DomainError):
    """The requested state change is not allowed (also on CAS loss)."""