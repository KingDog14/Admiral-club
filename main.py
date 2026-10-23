#!/usr/bin/env python3
"""Запуск Telegram-бота компьютерного клуба ADMIRAL.

Команды::

    python main.py              # запуск бота (polling)
    python main.py --init-db    # только создать/проверить базу данных
    python main.py --version    # версия

Перед запуском нужен файл ``.env`` (см. ``.env.example``) с ``BOT_TOKEN``
и ``ADMIN_ID``. База данных создаётся автоматически при первом старте.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, ErrorEvent

from bot import __version__
from bot.config import AppConfig, ConfigError, load_config
from bot.db.cli import create_database
from bot.db.database import Database, init_database
from bot.handlers import admin_router, client_router

logger = logging.getLogger("admiral.bot")

#: Команды, видимые всем пользователям в меню клиента Telegram.
COMMON_BOT_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand(command="start", description="Главное меню клуба"),
    BotCommand(command="help", description="Как забронировать место"),
    BotCommand(command="cancel", description="Отменить бронирование"),
    BotCommand(command="privacy", description="Персональные данные (152-ФЗ)"),
)

#: Дополнительная команда, видимая только в чате администратора.
ADMIN_BOT_COMMAND: BotCommand = BotCommand(
    command="admin",
    description="Админ-панель: статистика и активные брони",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Разобрать аргументы командной строки."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Telegram-бот компьютерного клуба: бронирование, чеки, админ-панель.",
    )
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="создать базу данных и применить схему, не запуская бота (токен не нужен)",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="показать версию и выйти",
    )
    return parser.parse_args(argv)


def setup_logging(level: str) -> None:
    """Настроить логирование: формат с временем и уровнем, тишина для aiohttp."""
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        stream=sys.stdout,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiogram").setLevel(logging.INFO)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    # Сторонние библиотеки не должны забивать лог клуба.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def _setup_bot_commands(bot: Bot, cfg: AppConfig) -> None:
    """Прописать команды в меню Telegram (для админа — расширенный набор)."""
    try:
        await bot.set_my_commands(list(COMMON_BOT_COMMANDS))
        await bot.set_my_commands(
            [*COMMON_BOT_COMMANDS, ADMIN_BOT_COMMAND],
            scope=BotCommandScopeChat(chat_id=cfg.admin_id),
        )
    except TelegramAPIError as exc:
        logger.warning("Не удалось обновить список команд бота: %s", exc)


async def run_polling(cfg: AppConfig) -> None:
    """Собрать бота, диспетчер и запустить long polling."""
    database = await init_database(cfg.db_path, cfg.timezone)

    bot = Bot(
        token=cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=MemoryStorage(), db=database, cfg=cfg)

    # Админ-панель подключается первой: её хендлеры имеют приоритет.
    dispatcher.include_router(admin_router)
    dispatcher.include_router(client_router)

    @dispatcher.startup()
    async def on_startup(bot: Bot, db: Database, cfg: AppConfig) -> None:  # noqa: A002
        # Неверный токен — единственная причина остановить запуск сразу.
        me = await bot.get_me()  # TelegramUnauthorizedError при неверном BOT_TOKEN
        logger.info("Бот @%s (id=%s) запущен, версия %s", me.username, me.id, __version__)
        logger.info("Администратор: id=%s", cfg.admin_id)
        logger.info("База данных: %s", db.path)
        logger.info("Часовой пояс клуба: %s", cfg.timezone)
        logger.info(
            "Прайс и правила клуба настраиваются в файле bot/texts.py "
            "(константы TARIFFS, CLUB_*, ADMIN_*)."
        )
        await _setup_bot_commands(bot, cfg)

    @dispatcher.shutdown()
    async def on_shutdown() -> None:
        logger.info("Останавливаю бота…")

    @dispatcher.errors()
    async def on_unhandled_error(event: ErrorEvent) -> None:
        """Логировать любую необработанную ошибку, не роняя polling."""
        update_id = getattr(event.update, "update_id", "?")
        logger.exception("Необработанная ошибка в апдейте %s: %s", update_id, event.exception)

    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        # Закрываем ресурсы и тогда, когда polling отработал штатно,
        # и тогда, когда запуск упал ещё в startup-хуке (например, неверный токен).
        await dispatcher.storage.close()
        await bot.session.close()
        await database.close()


def main(argv: list[str] | None = None) -> int:
    """Точка входа: разбор аргументов, загрузка ``.env``, запуск."""
    args = parse_args(argv)

    if args.version:
        print(f"Telegram-бот ПК-клуба ADMIRAL, версия {__version__}")
        return 0

    if args.init_db:
        try:
            asyncio.run(create_database())
        except ConfigError as exc:
            print(f"❌ Ошибка конфигурации:\n\n{exc}\n", file=sys.stderr)
            return 1
        return 0

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"❌ Ошибка конфигурации:\n\n{exc}\n", file=sys.stderr)
        return 1

    setup_logging(cfg.log_level)
    logger.info("Запуск бота ПК-клуба ADMIRAL (версия %s)…", __version__)

    try:
        asyncio.run(run_polling(cfg))
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
    except TelegramUnauthorizedError:
        logger.error("Telegram отклонил BOT_TOKEN — бот не запущен")
        print(
            "\n❌ Неверный BOT_TOKEN: Telegram вернул «Unauthorized».\n"
            "   Проверьте .env — токен нужно скопировать у @BotFather целиком,\n"
            "   без кавычек, пробелов и переносов строк. Если токен отозван —\n"
            "   выпустите новый командой /revoke у @BotFather.\n",
            file=sys.stderr,
        )
        return 1
    except TelegramNetworkError as exc:
        logger.error("Нет связи с api.telegram.org: %s", exc)
        print(
            "\n❌ Не удалось связаться с Telegram (проверьте интернет и доступ\n"
            "   к api.telegram.org, а также настройки прокси/файрвола).\n",
            file=sys.stderr,
        )
        return 1
    except TelegramAPIError as exc:
        # Чаще всего это конфликт polling-процессов: бот уже запущен где-то ещё.
        logger.error("Ошибка Telegram API, бот остановлен: %s", exc)
        print(
            "\n❌ Telegram API отклонил запрос. Частые причины:\n"
            "  • запущен второй экземпляр бота с тем же токеном\n"
            "    («Conflict: terminated by other getUpdates request») — остановите его;\n"
            "  • на бота установлен webhook — сбросьте его: @BotFather или\n"
            "    вызов deleteWebhook.\n",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
