"""Загрузка и валидация настроек бота из ``.env``.

В ``.env`` обязательны два значения (см. ``.env.example``):

* ``BOT_TOKEN`` — токен бота от @BotFather;
* ``ADMIN_ID``  — числовой Telegram ID администратора клуба.

Дополнительно можно переопределить ``DB_PATH``, ``TZ`` и ``LOG_LEVEL``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

# Корень проекта (…/Admiral-club) — от него считаем пути к .env и базе данных.
BASE_DIR: Path = Path(__file__).resolve().parent.parent
ENV_FILE: Path = BASE_DIR / ".env"
DEFAULT_DB_PATH: Path = BASE_DIR / "data" / "admiral.db"
DEFAULT_TZ: str = "Asia/Sakhalin"
DEFAULT_LOG_LEVEL: str = "INFO"


class ConfigError(RuntimeError):
    """Настройки бота неполные или некорректные — запуск невозможен."""


@dataclass(frozen=True, slots=True)
class DbSettings:
    """Настройки базы данных — нужны даже когда бот не запускается (``--init-db``)."""

    path: Path
    timezone: ZoneInfo


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Неизменяемый снимок настроек приложения."""

    bot_token: str
    admin_id: int
    db_path: Path
    timezone: ZoneInfo
    log_level: str = DEFAULT_LOG_LEVEL


def _read_token() -> str:
    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        raise ConfigError(
            "Не найден BOT_TOKEN.\n"
            f"1) Скопируйте {ENV_FILE.name}.example в {ENV_FILE.name}\n"
            "2) Получите токен у @BotFather командой /newbot\n"
            f"3) Впишите его в {ENV_FILE.name} строкой: BOT_TOKEN=123456:ABC-DEF..."
        )
    if ":" not in token:
        raise ConfigError(
            "BOT_TOKEN выглядит неверно: ожидался формат «123456789:AAExxxxxxxxxxx». "
            "Проверьте, что скопировали токен целиком, без кавычек и пробелов."
        )
    return token


def _read_admin_id() -> int:
    raw = (os.getenv("ADMIN_ID") or "").strip()
    if not raw:
        raise ConfigError(
            "Не найден ADMIN_ID — числовой Telegram ID администратора.\n"
            "Узнать свой ID можно у бота @userinfobot, затем вписать в .env: ADMIN_ID=123456789"
        )
    try:
        admin_id = int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"ADMIN_ID должен быть числом (например 123456789), получено: {raw!r}. "
            "Это не @username, а именно числовой ID — узнайте его у @userinfobot."
        ) from exc
    if admin_id <= 0:
        raise ConfigError(f"ADMIN_ID должен быть положительным числом, получено: {admin_id}")
    return admin_id


def _read_db_path() -> Path:
    raw = (os.getenv("DB_PATH") or "").strip()
    if not raw:
        return DEFAULT_DB_PATH
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def _read_timezone() -> ZoneInfo:
    raw = (os.getenv("TZ") or "").strip() or DEFAULT_TZ
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(
            f"Неизвестный часовой пояс TZ={raw!r}. Пример корректного значения: "
            f"{DEFAULT_TZ}, Europe/Moscow, Asia/Novosibirsk."
        ) from exc


def _read_log_level() -> str:
    raw = (os.getenv("LOG_LEVEL") or "").strip().upper() or DEFAULT_LOG_LEVEL
    allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
    if raw not in allowed:
        raise ConfigError(f"LOG_LEVEL должен быть одним из {sorted(allowed)}, получено: {raw!r}")
    return raw


def load_db_settings(env_file: str | Path | None = None) -> DbSettings:
    """Прочитать из окружения только путь к базе и часовой пояс.

    Используется командой инициализации базы (``python main.py --init-db``),
    где токен бота не нужен.
    """
    load_dotenv(dotenv_path=Path(env_file) if env_file else ENV_FILE, override=False)
    return DbSettings(path=_read_db_path(), timezone=_read_timezone())


def load_config(env_file: str | Path | None = None) -> AppConfig:
    """Прочитать ``.env`` и собрать :class:`AppConfig`.

    Реальные переменные окружения имеют приоритет над файлом (``override=False``),
    поэтому бота можно запускать в Docker/systemd без ``.env``.
    """
    db_settings = load_db_settings(env_file)

    return AppConfig(
        bot_token=_read_token(),
        admin_id=_read_admin_id(),
        db_path=db_settings.path,
        timezone=db_settings.timezone,
        log_level=_read_log_level(),
    )
