"""Контроль согласия на обработку персональных данных (152-ФЗ).

Middleware работает как «турникет» на клиентском роутере: пока у пользователя в
таблице ``users`` стоит ``consent_given = 0``, ни один сценарий (меню, прайс,
бронирование) ему недоступен. Пропускаются только три случая:

1. команда ``/start`` — по ней показывается текст согласия и кнопка;
2. команда ``/privacy`` — пользователь вправе прочитать условия и без согласия;
3. нажатие кнопки «✅ Я согласен» (``callback_data = consent:give``).

Остальные события получают вежливое напоминание. Middleware регистрируется как
``outer_middleware`` роутера, то есть выполняется ДО подбора хендлера — иначе
сообщение успело бы обработаться в обход требования закона.

Согласие кэшируется в памяти процесса (``self._consented``), чтобы не ходить в
базу на каждом сообщении; источник истины при этом остаётся SQLite.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from bot import texts
from bot.db.database import Database
from bot.keyboards.inline import CB_CONSENT_GIVE, consent_keyboard

logger = logging.getLogger(__name__)

#: Команды, которые доступны пользователю до получения согласия.
ALLOWED_COMMANDS_BEFORE_CONSENT: frozenset[str] = frozenset({"/start", "/privacy"})


class ConsentMiddleware(BaseMiddleware):
    """Не пускает пользователя к сценариям бота без согласия на обработку ПДн."""

    def __init__(self) -> None:
        self._consented: set[int] = set()

    def mark_consented(self, user_id: int) -> None:
        """Отметить пользователя как согласившегося (вызывается хендлером кнопки)."""
        self._consented.add(user_id)

    def forget(self, user_id: int) -> None:
        """Сбросить кэш согласия — например, после отзыва согласия пользователем."""
        self._consented.discard(user_id)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        db: Database | None = data.get("db")
        sender: User | None = data.get("event_from_user")
        chat = data.get("event_chat")

        # Нет контекста (канал, служебное событие) или нет базы — не блокируем,
        # остальные проверки выполнят фильтры роутера.
        if db is None or sender is None or chat is None or chat.type != "private":
            return await handler(event, data)

        if sender.id in self._consented:
            return await handler(event, data)

        if await db.has_consent(sender.id):
            self._consented.add(sender.id)
            return await handler(event, data)

        return await self._block_until_consent(handler, event, data)

    async def _block_until_consent(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Ответить пользователю напоминанием и не вызывать хендлер."""
        db: Database = data["db"]
        sender: User = data["event_from_user"]

        # Заодно сохраняем профиль: он понадобится, как только согласие дадут.
        await db.upsert_user(sender.id, sender.username, sender.first_name)

        if isinstance(event, CallbackQuery):
            if event.data == CB_CONSENT_GIVE:
                return await handler(event, data)
            await event.answer(
                "⛔️ Сначала подтвердите согласие на обработку персональных "
                "данных — отправьте /start.",
                show_alert=True,
            )
            return None

        if isinstance(event, Message):
            first_word = (event.text or "").strip().split(maxsplit=1)[0] if event.text else ""
            if first_word in ALLOWED_COMMANDS_BEFORE_CONSENT:
                return await handler(event, data)
            await event.answer(texts.CONSENT_REQUIRED, reply_markup=consent_keyboard())
            return None

        logger.debug("Событие %s отклонено: нет согласия на обработку ПДн", type(event).__name__)
        return None


__all__ = ["ALLOWED_COMMANDS_BEFORE_CONSENT", "ConsentMiddleware"]
