"""Фильтры доступа: администраторы, личные сообщения."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Chat, Message

from bot.config import AppConfig
from bot.db.database import Database
from bot.services.admins import AdminRegistry

logger = logging.getLogger(__name__)

#: События, у которых есть ``from_user``: сообщения и колбэки.
AccessEvent = Message | CallbackQuery


class IsAdmin(BaseFilter):
    """Пропускать события только от администраторов клуба.

    Администратор — активный пользователь из таблицы ``admins``. Список
    администраторов задаётся в ``.env`` (``ADMIN_IDS``) и расширяется через
    админ-панель, поэтому проверка идёт по базе, а не только по конфигу.

    Порядок источников:

    1. ``admins`` — :class:`bot.services.admins.AdminRegistry` (кэш в памяти,
       основной путь в работающем боте); его вердикт окончательный;
    2. ``db`` — прямой запрос к базе, если реестр не передан;
    3. ``cfg`` — запасной вариант: окружения без базы (тесты, миграции) или
       база, в которой ещё нет ни одного администратора.
    """

    name = "is_admin"

    async def __call__(
        self,
        event: AccessEvent,
        *,
        cfg: AppConfig | None = None,
        db: Database | None = None,
        admins: AdminRegistry | None = None,
    ) -> bool:
        user_id = event.from_user.id if event.from_user else None
        if not user_id:
            return False

        if admins is not None:
            try:
                allowed = await admins.is_admin(user_id)
            except Exception:
                logger.exception(
                    "Сбой реестра администраторов при проверке %s", user_id
                )
            else:
                if not allowed:
                    logger.debug("Пользователь %s не администратор (реестр)", user_id)
                return allowed

        if db is not None:
            try:
                if await db.is_active_admin(user_id):
                    return True
                total, _ = await db.count_admins()
            except Exception:
                logger.exception("Сбой проверки администратора %s в базе", user_id)
            else:
                if total:
                    logger.debug("Пользователь %s не администратор (база)", user_id)
                    return False

        # Ни реестра, ни базы (или таблица admins пуста) — сверяемся с .env.
        allowed = cfg is not None and cfg.is_env_admin(user_id)
        if not allowed:
            logger.debug(
                "Пользователь %s не администратор — событие отклонено", user_id
            )
        return allowed


class IsPrivateChat(BaseFilter):
    """Пропускает только личные чаты с ботом."""

    name = "is_private_chat"

    async def __call__(self, event: Any, event_chat: Chat | None = None) -> bool:
        # event не используется: тип чата aiogram кладёт в контекст как event_chat.
        return event_chat is not None and event_chat.type == ChatType.PRIVATE


#: Готовые магические фильтры для переиспользования в роутерах.
admin_filter = IsAdmin()
private_filter = F.chat.type == ChatType.PRIVATE

__all__ = ["AccessEvent", "IsAdmin", "IsPrivateChat", "admin_filter", "private_filter"]
