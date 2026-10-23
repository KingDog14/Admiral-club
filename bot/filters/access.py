"""Фильтры доступа.

:class:`IsAdmin` сверяет ID отправителя с ``ADMIN_ID`` из ``.env`` и вешается на
весь роутер админ-панели — любой, кто не администратор, до её хендлеров просто
не доходит (событие уходит дальше, в клиентский роутер).

:class:`IsPrivateChat` отсекает группы и каналы: бот рассчитан на личные чаты,
а уведомления о решении по брони отправляются клиенту по ``user_id``, что
работает только в личном диалоге.
"""

from __future__ import annotations

from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Chat, Message

from bot.config import AppConfig

# Telegram-событие, к которому применимы фильтры доступа.
AccessEvent = Message | CallbackQuery


class IsAdmin(BaseFilter):
    """Пропускает только администратора клуба (``ADMIN_ID`` из ``.env``)."""

    async def __call__(self, event: AccessEvent, cfg: AppConfig | None = None) -> bool:
        if cfg is None:  # конфигурация не передана в workflow data — доступ запрещаем
            return False
        sender = event.from_user
        return sender is not None and sender.id == cfg.admin_id


class IsPrivateChat(BaseFilter):
    """Пропускает только личные чаты с ботом."""

    async def __call__(self, event: Any, event_chat: Chat | None = None) -> bool:  # noqa: ARG002
        # event не используется: тип чата aiogram кладёт в контекст как event_chat.
        return event_chat is not None and event_chat.type == "private"


__all__ = ["IsAdmin", "IsPrivateChat"]
