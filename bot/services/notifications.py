"""Отправка уведомлений клиенту и администратору.

Любая отправка может упасть по сетевым причинам или потому, что пользователь
заблокировал бота (``TelegramForbiddenError``). Бот при этом не должен
останавливаться, а администратор — видеть честный статус доставки, поэтому все
вызовы обёрнуты в try/except и возвращают ``bool``.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from bot import texts
from bot.config import AppConfig
from bot.db.models import Booking, User
from bot.keyboards.inline import booking_decision_keyboard
from bot.utils.formatting import escape_html, format_datetime, truncate

logger = logging.getLogger(__name__)

#: Ограничения Telegram на длину подписи к медиа и текста сообщения.
CAPTION_LIMIT: int = 1024
MESSAGE_LIMIT: int = 4096


async def deliver_new_booking(
    bot: Bot,
    cfg: AppConfig,
    booking: Booking,
    user: User | None = None,
) -> bool:
    """Отправить администратору чек и кнопки решения по новой брони.

    Возвращает ``True``, если уведомление доставлено.
    """
    user_link = user.link if user is not None else f"ID: <code>{booking.user_id}</code>"
    caption = texts.NEW_BOOKING_CAPTION.format(
        booking_id=booking.id,
        tariff=escape_html(booking.tariff),
        time_slot=escape_html(booking.time_slot),
        client_name=escape_html(booking.client_name),
        user_link=user_link,
        created_at=format_datetime(booking.created_at, cfg.timezone),
    )
    reply_markup = booking_decision_keyboard(booking.id)

    try:
        if booking.receipt_file_id:
            await bot.send_photo(
                chat_id=cfg.admin_id,
                photo=booking.receipt_file_id,
                caption=truncate(caption, CAPTION_LIMIT),
                reply_markup=reply_markup,
            )
        else:
            await bot.send_message(
                chat_id=cfg.admin_id,
                text=truncate(caption, MESSAGE_LIMIT),
                reply_markup=reply_markup,
            )
    except TelegramForbiddenError:
        logger.error(
            "Администратор %s заблокировал бота или не начинал с ним диалог — "
            "уведомление о брони №%s не доставлено",
            cfg.admin_id,
            booking.id,
        )
        return False
    except TelegramAPIError:
        logger.exception(
            "Не удалось отправить уведомление администратору %s о брони №%s",
            cfg.admin_id,
            booking.id,
        )
        return False

    logger.info("Уведомление о брони №%s отправлено администратору %s", booking.id, cfg.admin_id)
    return True


async def deliver_decision(bot: Bot, booking: Booking, approved: bool) -> bool:
    """Сообщить клиенту решение по его брони.

    Возвращает ``True``, если сообщение доставлено (``False`` — клиент
    заблокировал бота или возникла ошибка Telegram API).
    """
    if approved:
        text = texts.CLIENT_APPROVED.format(
            tariff=escape_html(booking.tariff),
            time_slot=escape_html(booking.time_slot),
            booking_id=booking.id,
            client_name=escape_html(booking.client_name),
        )
    else:
        text = texts.CLIENT_REJECTED.format(
            booking_id=booking.id,
            tariff=escape_html(booking.tariff),
            time_slot=escape_html(booking.time_slot),
        )

    try:
        await bot.send_message(chat_id=booking.user_id, text=truncate(text, MESSAGE_LIMIT))
    except TelegramForbiddenError:
        logger.warning(
            "Клиент %s заблокировал бота — решение по брони №%s не доставлено",
            booking.user_id,
            booking.id,
        )
        return False
    except TelegramAPIError:
        logger.exception(
            "Не удалось отправить клиенту %s решение по брони №%s",
            booking.user_id,
            booking.id,
        )
        return False

    logger.info(
        "Клиент %s уведомлён о решении по брони №%s: %s",
        booking.user_id,
        booking.id,
        "подтверждена" if approved else "отклонена",
    )
    return True


__all__ = ["CAPTION_LIMIT", "MESSAGE_LIMIT", "deliver_decision", "deliver_new_booking"]
