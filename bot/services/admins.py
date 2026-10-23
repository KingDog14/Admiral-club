"""Реестр администраторов: быстрая проверка доступа без запроса к базе на каждое сообщение.

Фильтр :class:`bot.filters.access.IsAdmin` срабатывает на *каждом* событии
админ-роутера, поэтому держать список активных администраторов в памяти заметно
быстрее, чем ходить в SQLite по каждому клиентскому сообщению.

Источник истины — таблица ``admins``. Кэш обновляется:

* принудительно после любого изменения списка (добавили / удалили / отключили /
  сменили роль) — метод :meth:`AdminRegistry.reload`;
* автоматически при старте бота — метод :meth:`AdminRegistry.startup`;
* по истечении ``ttl_seconds`` — защита на случай, если базу правили снаружи
  (например, вручную через ``sqlite3``).
"""

from __future__ import annotations

import asyncio
import logging
import time

from bot.config import AppConfig
from bot.db.database import Database
from bot.db.models import AdminRole

logger = logging.getLogger(__name__)

#: Через сколько секунд кэш перечитывается из базы сам.
DEFAULT_TTL_SECONDS: float = 60.0


class AdminRegistry:
    """Кэш активных администраторов клуба."""

    def __init__(
        self,
        db: Database,
        cfg: AppConfig,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._db = db
        self._cfg = cfg
        self._ttl = ttl_seconds
        # dict вместо set: сохраняет порядок из базы (владелец — первым).
        self._ids: dict[int, None] = {}
        self._loaded_at: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Чтение
    # ------------------------------------------------------------------ #
    def _expired(self) -> bool:
        return time.monotonic() - self._loaded_at > self._ttl

    async def reload(self) -> tuple[int, ...]:
        """Принудительно перечитать список активных администраторов из базы."""
        async with self._lock:
            try:
                self._ids = dict.fromkeys(await self._db.active_admin_ids())
            except Exception:
                logger.exception(
                    "Не удалось прочитать администраторов из базы — "
                    "оставляю предыдущий кэш (%s)",
                    list(self._ids),
                )
                return tuple(self._ids)
            self._loaded_at = time.monotonic()
            logger.debug("Реестр администраторов обновлён: %s", list(self._ids))
            return tuple(self._ids)

    async def active_ids(self) -> tuple[int, ...]:
        """Активные администраторы (с учётом TTL-обновления)."""
        if not self._ids or self._expired():
            await self.reload()
        return tuple(self._ids)

    async def is_admin(self, user_id: int | None) -> bool:
        """Проверка доступа: является ли пользователь активным администратором.

        Если таблица ``admins`` пуста (база создана вручную или очищена),
        список восстанавливается из ``.env`` — бот не остаётся без управления.
        Отключённый через панель администратор доступа не получает: решение
        базы считается окончательным.
        """
        if not user_id:
            return False
        if not self._ids or self._expired():
            await self.reload()
        if user_id in self._ids:
            return True
        if not self._ids:
            logger.warning("В базе нет активных администраторов — восстанавливаю список из .env")
            await self.startup()
            return user_id in self._ids
        return False

    async def is_owner(self, user_id: int | None) -> bool:
        """Проверка права управлять списком администраторов (роль ``owner``)."""
        if not user_id or not await self.is_admin(user_id):
            return False
        admin = await self._db.get_admin(user_id)
        return admin is not None and admin.is_owner

    async def role_title(self, user_id: int | None) -> str:
        """Человеческое название роли администратора (для приветствия)."""
        if not user_id:
            return AdminRole.title(AdminRole.OPERATOR)
        admin = await self._db.get_admin(user_id)
        return admin.role_title if admin is not None else AdminRole.title(AdminRole.OPERATOR)

    # ------------------------------------------------------------------ #
    # Запись
    # ------------------------------------------------------------------ #
    async def startup(self) -> tuple[int, ...]:
        """Вызывается при запуске: синхронизировать ``.env`` с базой и загрузить кэш."""
        await self._db.sync_env_admins(self._cfg.admin_ids)
        return await self.reload()

    def remember(self, user_id: int) -> None:
        """Отметить администратора активным в кэше (после добавления/включения)."""
        self._ids[user_id] = None

    def forget(self, user_id: int) -> None:
        """Убрать администратора из кэша (после удаления/отключения)."""
        self._ids.pop(user_id, None)

    @property
    def cfg(self) -> AppConfig:
        return self._cfg

    @property
    def db(self) -> Database:
        return self._db


__all__ = ["DEFAULT_TTL_SECONDS", "AdminRegistry"]
