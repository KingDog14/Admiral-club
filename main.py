#!/usr/bin/env python3
"""Запуск демонстрационного Telegram-бота кибер-клуба NOVA ARENA.

Команды::

    python main.py              # запуск бота (polling)
    python main.py --init-db    # только создать/проверить базу данных
    python main.py --version    # версия

Перед запуском нужен файл ``.env`` (см. ``.env.example``) с ``BOT_TOKEN``
и ``ADMIN_IDS``. База данных создаётся автоматически при первом старте,
администраторы из ``.env`` — тоже (таблица ``admins``).
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

from bot import __version__, texts
from bot.config import AppConfig, ClubInfo, ConfigError, load_config, read_club_info
from bot.db.cli import create_database
from bot.db.database import Database, init_database
from bot.handlers import admin_router, client_router
from bot.services.admins import AdminRegistry

logger = logging.getLogger("nova.bot")

#: Команды, видимые всем пользователям в меню клиента Telegram.
#: Админ-панель открывается кнопкой в главном меню (её видят только
#: администраторы), поэтому в меню команд её выносить не нужно.
COMMON_BOT_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand(command="start", description="Главное меню клуба"),
    BotCommand(command="help", description="Как забронировать место"),
    BotCommand(command="cancel", description="Отменить бронирование"),
    BotCommand(command="privacy", description="Персональные данные (152-ФЗ)"),
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
        "--print-profile",
        action="store_true",
        help="вывести готовые тексты About, Description и путь к картинке 640×360 для @BotFather",
    )
    parser.add_argument(
        "--print-palette",
        action="store_true",
        help="показать 10-тоновую палитру inline-кнопок и правила поверхностей 60–30–10",
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


async def _setup_bot_commands(bot: Bot, admin_ids: tuple[int, ...]) -> None:
    """Прописать команды в меню Telegram и убрать старые команды у админов."""
    try:
        await bot.set_my_commands(list(COMMON_BOT_COMMANDS))
    except TelegramAPIError as exc:
        logger.warning("Не удалось обновить общий список команд бота: %s", exc)

    # Сбросить персональные меню администраторов от предыдущих версий бота.
    for admin_id in admin_ids:
        try:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramAPIError as exc:
            logger.debug("Не удалось сбросить меню команд для %s: %s", admin_id, exc)


async def _setup_bot_profile(bot: Bot, club: ClubInfo) -> None:
    """Синхронизировать поля About и Description профиля бота через Bot API."""
    about = texts.build_bot_about(club)
    description = texts.build_bot_description(club)
    try:
        await bot.set_my_short_description(short_description=about)
    except TelegramAPIError as exc:
        logger.warning("Не удалось обновить About (short_description) бота: %s", exc)
    try:
        await bot.set_my_description(description=description)
    except TelegramAPIError as exc:
        logger.warning("Не удалось обновить Description бота: %s", exc)


async def run_polling(cfg: AppConfig) -> None:
    """Собрать бота, диспетчер и запустить long polling."""
    database = await init_database(cfg.db_path, cfg.timezone)

    bot = Bot(
        token=cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Реестр администраторов: проверка доступа по таблице admins без запроса
    # к базе на каждое сообщение. Передаётся в workflow_data как ``admins``.
    admins = AdminRegistry(database, cfg)
    dispatcher = Dispatcher(
        storage=MemoryStorage(), db=database, cfg=cfg, admins=admins
    )

    # Админ-панель подключается первой: её хендлеры имеют приоритет.
    dispatcher.include_router(admin_router)
    dispatcher.include_router(client_router)

    @dispatcher.startup()
    async def on_startup(
        bot: Bot, db: Database, cfg: AppConfig, admins: AdminRegistry
    ) -> None:
        # Неверный токен — единственная причина остановить запуск сразу.
        me = await bot.get_me()  # TelegramUnauthorizedError при неверном BOT_TOKEN
        logger.info(
            "Бот @%s (id=%s) запущен, версия %s", me.username, me.id, __version__
        )

        # Синхронизируем ADMIN_IDS из .env с таблицей admins и читаем кэш.
        admin_ids = await admins.startup()
        logger.info(
            "Администраторов активно: %d — %s",
            len(admin_ids),
            ", ".join(map(str, admin_ids)) or "нет",
        )
        logger.info("Владелец бота (первый ADMIN_IDS): id=%s", cfg.admin_id)
        logger.info("База данных: %s", db.path)
        logger.info("Часовой пояс клуба: %s", cfg.timezone)
        club = cfg.club
        logger.info(
            "Клуб: %s, %s — %s, работает %s",
            club.name,
            club.city,
            club.address,
            club.hours,
        )
        logger.info(
            "Администратор: %s, телефон %s; предоплата: %s %s (%s)",
            club.admin_name,
            club.admin_phone,
            club.payment_system,
            club.payment_card,
            club.payment_holder,
        )
        logger.info(
            "Цены и правила — в bot/texts.py (TARIFFS); контакты клуба меняются "
            "через .env (CLUB_*, ADMIN_NAME, ADMIN_PHONE, PAYMENT_*)."
        )
        await _setup_bot_commands(bot, admin_ids)
        await _setup_bot_profile(bot, club)
        from bot.ui.emoji_pack import sync_inline_emoji_pack

        await sync_inline_emoji_pack(bot, owner_id=cfg.admin_id, club_name=club.name)

    @dispatcher.shutdown()
    async def on_shutdown() -> None:
        logger.info("Останавливаю бота…")

    @dispatcher.errors()
    async def on_unhandled_error(event: ErrorEvent) -> None:
        """Логировать любую необработанную ошибку, не роняя polling."""
        update_id = getattr(event.update, "update_id", "?")
        logger.exception(
            "Необработанная ошибка в апдейте %s: %s", update_id, event.exception
        )

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
        print(f"Telegram-бот кибер-клуба NOVA ARENA (демо), версия {__version__}")
        return 0

    if args.print_palette:
        from bot.ui.palette import format_palette_summary

        print(format_palette_summary())
        return 0

    if args.print_profile:
        from dotenv import load_dotenv

        from bot.config import BASE_DIR, ENV_FILE

        load_dotenv(dotenv_path=ENV_FILE, override=False)
        club = read_club_info()
        about = texts.build_bot_about(club)
        desc = texts.build_bot_description(club)
        print(
            f"=== ABOUT (/setabouttext, {len(about)}/{texts.BOT_ABOUT_LIMIT} симв.) ==="
        )
        print(about)
        print(
            f"\n=== DESCRIPTION (/setdescription, "
            f"{len(desc)}/{texts.BOT_DESCRIPTION_LIMIT} симв.) ==="
        )
        print(desc)
        print("\n=== DESCRIPTION PICTURE (/setdescriptionpic, 640×360) ===")
        print(f"PNG:  {BASE_DIR / 'bot' / 'assets' / 'description_pic.png'}")
        print(f"JPEG: {BASE_DIR / 'bot' / 'assets' / 'description_pic.jpg'}")
        print(f"HD:   {BASE_DIR / 'bot' / 'assets' / 'description_pic_hd.png'} (1280×720)")
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
    logger.info("Запуск бота кибер-клуба %s (версия %s)…", cfg.club.name, __version__)

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
