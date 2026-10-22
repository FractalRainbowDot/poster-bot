"""Reusable service card presentation; business effects are never retried."""
from aiogram import Bot

from .navigation import NavigationStore


async def delete_best_effort(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass  # cosmetic cleanup must not undo a committed flow transition


async def present(bot: Bot, chat_id: int, text: str, markup,
                  store: NavigationStore, *, now: int) -> None:
    old = store.get(chat_id)
    # Editing cannot move a card below incoming content/preview, nor recreate
    # history cleared only on the owner's client. Always replace at the bottom.
    sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    try:
        store.set(chat_id, sent.message_id, now=now)
    except Exception:
        await delete_best_effort(bot, chat_id, sent.message_id)
        raise
    if old is not None:
        await delete_best_effort(bot, chat_id, old)
