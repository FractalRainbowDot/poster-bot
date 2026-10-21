"""Channel transport boundary (PLAN §2, §4 step 5).

The ports module is dependency-free; the aiogram-backed adapter lives in
``transport.aiogram_channel`` and must only be imported for live composition,
never from the app import path.
"""

from poster_bot.transport.ports import (
    DefinitiveFailure,
    PublishOutcome,
    Sent,
    TransportCallError,
    UncertainOutcome,
)

__all__ = [
    "DefinitiveFailure",
    "PublishOutcome",
    "Sent",
    "TransportCallError",
    "UncertainOutcome",
]