"""Загрузка и валидация настроек бота из ``.env``.

В ``.env`` обязательны два значения (см. ``.env.example``):

* ``BOT_TOKEN``  — токен бота от @BotFather;
* ``ADMIN_IDS``  — числовые Telegram ID администраторов клуба через запятую.
  Первый ID считается **владельцем** бота: он управляет списком администраторов
  и не может быть удалён из панели. Переменная ``ADMIN_ID`` (один ID) тоже
  поддерживается — для обратной совместимости.

Дополнительно можно переопределить ``DB_PATH``, ``TZ`` и ``LOG_LEVEL``.

``.env`` задаёт лишь стартовый набор администраторов: остальных можно добавлять
и отключать прямо в админ-панели, они хранятся в таблице ``admins`` базы данных.
"""

from __future__ import annotations

import os
import re
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
    admin_ids: tuple[int, ...]
    db_path: Path
    timezone: ZoneInfo
    log_level: str = DEFAULT_LOG_LEVEL

    @property
    def admin_id(self) -> int:
        """Владелец бота — первый ID из ``ADMIN_IDS``.

        Свойство оставлено для совместимости с кодом, рассчитанным на одного
        администратора (например, персональный набор команд в меню Telegram).
        """
        return self.admin_ids[0]

    def is_env_admin(self, user_id: int) -> bool:
        """Входит ли пользователь в список администраторов из ``.env``."""
        return user_id in self.admin_ids


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


#: Разделители списка ID в ADMIN_IDS: запятая, точка с запятой, пробел, новая строка.
_ADMIN_IDS_SPLIT = re.compile(r"[,;\s]+")

_ID_HINT = (
    "Это не @username, а именно числовой ID — узнать его можно у бота @userinfobot."
)


def _read_admin_ids() -> tuple[int, ...]:
    """Прочитать список ID администраторов: ``ADMIN_IDS`` или устаревший ``ADMIN_ID``."""
    raw = (os.getenv("ADMIN_IDS") or "").strip()
    variable = "ADMIN_IDS"
    if not raw:
        raw = (os.getenv("ADMIN_ID") or "").strip()
        variable = "ADMIN_ID"
    if not raw:
        raise ConfigError(
            "Не найден ADMIN_IDS — числовые Telegram ID администраторов клуба.\n"
            "Узнайте ID у бота @userinfobot и впишите в .env, например:\n"
            "  ADMIN_IDS=123456789,987654321   # первый ID — владелец бота\n"
            "Для одного администратора достаточно ADMIN_IDS=123456789."
        )

    admin_ids: list[int] = []
    for chunk in _ADMIN_IDS_SPLIT.split(raw):
        if not chunk:
            continue
        try:
            value = int(chunk)
        except ValueError as exc:
            raise ConfigError(
                f"{variable}: «{chunk}» не похоже на числовой Telegram ID. {_ID_HINT}"
            ) from exc
        if value <= 0:
            raise ConfigError(
                f"{variable}: ID должен быть положительным числом, получено {value}. {_ID_HINT}"
            )
        if value not in admin_ids:  # сохраняем порядок и убираем дубликаты
            admin_ids.append(value)

    if not admin_ids:
        raise ConfigError(f"{variable} заполнен, но не содержит ни одного ID. {_ID_HINT}")
    return tuple(admin_ids)


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
        admin_ids=_read_admin_ids(),
        db_path=db_settings.path,
        timezone=db_settings.timezone,
        log_level=_read_log_level(),
    )
