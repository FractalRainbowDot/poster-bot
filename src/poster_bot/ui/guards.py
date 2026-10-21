"""Owner/private guard — the single authorization gate of the UI.

Everything owner-facing (state changes, previews, callback execution) runs
only after this gate passes: private chat, chat with the owner, sender is the
owner. It is enforced by the adapter BEFORE any flow call; forged callbacks
still hit flow-side status/version re-validation as the second guard.

A failing guard produces no reply at all: unauthorized traffic must never
learn anything private about the bot.
"""
from __future__ import annotations


def is_owner_private(
    *,
    chat_id: int,
    chat_type: str,
    user_id: int,
    owner_user_id: int,
) -> bool:
    """True only for the owner's private chat (chat with the owner AND the
    sender being the owner — group chats with the owner are still excluded:
    the bot's UI and data are private by requirement)."""
    return (
        chat_type == "private"
        and chat_id == owner_user_id
        and user_id == owner_user_id
    )