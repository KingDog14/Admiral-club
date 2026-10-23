"""Фильтры доступа: администратор и приватный чат."""

from bot.filters.access import IsAdmin, IsPrivateChat

__all__ = ["IsAdmin", "IsPrivateChat"]
