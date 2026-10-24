"""Безопасные обёртки над методами Telegram API.

Экран бота строится на графических карточках (``sendPhoto`` + ``editMessageMedia``):
при переходах меняется и баннер раздела, и подпись под ним, и inline-клавиатура.
Чтобы повторные клики работали без загрузки файлов по сети, после первой отправки
сохраняется серверный ``file_id`` фотографии в :data:`bot.ui.cards.card_cache`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InputMediaPhoto, ReplyKeyboardRemove

if TYPE_CHECKING:
    from bot.ui.cards import RenderedCard

logger = logging.getLogger(__name__)


def _is_not_modified(exc: BaseException) -> bool:
    return "message is not modified" in str(exc).lower()


def _extract_photo_file_id(message: Any) -> str | None:
    photos = getattr(message, "photo", None)
    if photos and isinstance(photos, (list, tuple)):
        fid = getattr(photos[-1], "file_id", None)
        if isinstance(fid, str) and fid:
            return fid
    return None


def _bot_key(bot: Bot) -> int:
    return int(getattr(bot, "id", 0) or id(bot))


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
        if _is_not_modified(exc):
            return True
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
        if _is_not_modified(exc):
            return True
        logger.debug("Не удалось отредактировать подпись %s/%s: %s", chat_id, message_id, exc)
        return False
    return True


async def safe_edit_message_media(
    bot: Bot,
    chat_id: int,
    message_id: int,
    card: RenderedCard,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Заменить графическую карточку, подпись и кнопки в существующем сообщении."""
    from bot.ui.cards import card_cache

    bkey = _bot_key(bot)
    cached_fid = card_cache.get_file_id(bkey, card.cache_key)

    for attempt_fid in ([cached_fid, None] if cached_fid else [None]):
        media_input: str | BufferedInputFile = (
            attempt_fid
            if attempt_fid is not None
            else BufferedInputFile(card.jpeg_bytes, filename=f"{card.cache_key}.jpg")
        )
        media = InputMediaPhoto(
            media=media_input,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
        try:
            edited = await bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=media,
                reply_markup=reply_markup,
            )
            fid = _extract_photo_file_id(edited)
            if fid:
                card_cache.put_file_id(bkey, card.cache_key, fid)
            return True
        except TelegramAPIError as exc:
            if _is_not_modified(exc):
                return True
            if attempt_fid is not None:
                card_cache.invalidate_file_id(bkey, card.cache_key)
                continue
            logger.debug("Не удалось обновить медиа-экран %s/%s: %s", chat_id, message_id, exc)
            return False
    return False


async def send_photo_screen(
    bot: Bot,
    chat_id: int,
    card: RenderedCard,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> int:
    """Отправить новую графическую карточку экрана и вернуть её ``message_id``."""
    from bot.ui.cards import card_cache

    bkey = _bot_key(bot)
    cached_fid = card_cache.get_file_id(bkey, card.cache_key)

    for attempt_fid in ([cached_fid, None] if cached_fid else [None]):
        photo_input: str | BufferedInputFile = (
            attempt_fid
            if attempt_fid is not None
            else BufferedInputFile(card.jpeg_bytes, filename=f"{card.cache_key}.jpg")
        )
        try:
            sent = await bot.send_photo(
                chat_id=chat_id,
                photo=photo_input,
                caption=caption,
                reply_markup=reply_markup,
            )
            fid = _extract_photo_file_id(sent)
            if fid:
                card_cache.put_file_id(bkey, card.cache_key, fid)
            return int(getattr(sent, "message_id", 0) or 0)
        except TelegramAPIError as exc:
            if attempt_fid is not None:
                card_cache.invalidate_file_id(bkey, card.cache_key)
                continue
            logger.exception("Не удалось отправить фото-экран пользователю %s: %s", chat_id, exc)
            raise
    return 0


async def edit_or_send(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Обновить текстовое сообщение, а если Telegram не позволил — отправить новое."""
    if await safe_edit_message_text(bot, chat_id, message_id, text, reply_markup):
        return
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


async def send_screen(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    clear_reply_keyboard: bool = False,
) -> int:
    """Текстовый запасной вариант отправки экрана."""
    if clear_reply_keyboard:
        sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=ReplyKeyboardRemove())
        message_id = int(getattr(sent, "message_id", 0) or 0)
        if message_id and await safe_edit_message_text(bot, chat_id, message_id, text, reply_markup):
            return message_id

    sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    return int(getattr(sent, "message_id", 0) or 0)


async def safe_delete_message(bot: Bot, chat_id: int, message_id: int) -> bool:
    """Удалить сообщение, если Telegram это позволяет (не старше 48 часов)."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError as exc:
        logger.debug("Не удалось удалить сообщение %s/%s: %s", chat_id, message_id, exc)
        return False
    return True
