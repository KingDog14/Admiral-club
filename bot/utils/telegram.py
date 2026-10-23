"""Безопасные обёртки над методами Telegram API.

Админ может нажать кнопку под старым сообщением, сообщение может оказаться
недоступным (``InaccessibleMessage``) или текст может не измениться — всё это
обычные ситуации, из-за которых бот не должен падать. Поэтому правки сообщений
обёрнуты в try/except и возвращают ``bool`` (получилось или нет).
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup

logger = logging.getLogger(__name__)


async def safe_edit_message_text(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Отредактировать текст сообщения; вернуть ``True`` при успехе."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )
    except TelegramAPIError as exc:
        # «message is not modified», «message can't be edited», «message to edit not found»
        logger.debug("Не удалось отредактировать сообщение %s/%s: %s", chat_id, message_id, exc)
        return False
    return True


async def safe_edit_message_caption(
    bot: Bot,
    chat_id: int,
    message_id: int,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Отредактировать подпись под фото (чек); вернуть ``True`` при успехе."""
    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=caption,
            reply_markup=reply_markup,
        )
    except TelegramAPIError as exc:
        logger.debug("Не удалось отредактировать подпись %s/%s: %s", chat_id, message_id, exc)
        return False
    return True


async def edit_or_send(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Обновить сообщение шага, а если Telegram не позволил — отправить новое.

    Так сценарий не «зависает»: например, сообщение могло стать недоступным
    (``InaccessibleMessage`` — старше 48 часов) или его текст не изменился.
    """
    if await safe_edit_message_text(bot, chat_id, message_id, text, reply_markup):
        return
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


async def safe_delete_message(bot: Bot, chat_id: int, message_id: int) -> bool:
    """Удалить сообщение, если Telegram это позволяет (не старше 48 часов)."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError as exc:
        logger.debug("Не удалось удалить сообщение %s/%s: %s", chat_id, message_id, exc)
        return False
    return True
