"""Создание базы данных без запуска бота.

    python -m bot.db.cli

Полезно при первой настройке сервера: команда создаёт файл базы, применяет
схему и печатает текущую статистику. Тот же функционал доступен как
``python main.py --init-db`` (в отличие от запуска бота, токен не требуется).
"""

from __future__ import annotations

import asyncio
import logging

from bot.config import load_db_settings
from bot.db.database import init_database

logger = logging.getLogger(__name__)


async def create_database() -> None:
    settings = load_db_settings()
    database = await init_database(settings.path, settings.timezone)
    try:
        stats = await database.get_stats()
        tables = await database.list_tables()
    finally:
        await database.close()

    print("✅ База данных готова.")
    print(f"   Файл:        {settings.path}")
    print(f"   Часовой пояс: {settings.timezone}")
    print(f"   Таблицы:      {', '.join(tables)}")
    print(f"   Пользователей: {stats.users} (согласие на ПДн: {stats.consented})")
    print(
        "   Заявок:        "
        f"{stats.total} (ожидают: {stats.pending}, "
        f"подтверждено: {stats.approved}, отклонено: {stats.rejected})"
    )
    print(f"   Администраторов: {stats.admins} (активных: {stats.admins_active})")
    print(
        "   Администраторы из .env появятся в таблице admins после первого запуска "
        "бота (python main.py)."
    )


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(create_database())


if __name__ == "__main__":
    main()
