"""Чистая логика сценария бронирования (без Telegram API — легко тестировать).

Здесь нормализуются данные, которые клиент вводит текстом:

* имя для брони — только буквы, 2–40 символов (никаких ссылок, эмодзи и цифр:
  администратор ищет гостя в клубе по имени);
* своё время — произвольный текст до 60 символов, но обязательно с цифрами;
* вариант времени из кнопок — превращается в человекочитаемую строку для БД.
"""

from __future__ import annotations

import re
from datetime import datetime

from bot import texts
from bot.keyboards.reply import ALL_MENU_BUTTONS

#: Ограничения на пользовательский ввод.
NAME_MIN_LEN: int = 2
NAME_MAX_LEN: int = 40
TIME_MAX_LEN: int = 60

# Буквы (включая кириллицу), пробел, дефис, апостроф, точка — «Иван», «Анна-Мария», «И. И. Петров».
_NAME_PATTERN = re.compile(r"[^\W\d_]+(?:[ .'\-][^\W\d_]+)*", re.UNICODE)
# В тексте времени должна быть хотя бы одна цифра («18:30», «на 3 часа», «25.10»).
_HAS_DIGIT = re.compile(r"\d")


#: Варианты времени-пакета: подпись для базы и интервал из прайса клуба.
_PACKAGES: dict[str | None, tuple[str, str]] = {
    texts.TIME_MORNING_KEY: ("Утро", texts.MORNING_SLOT),
    texts.TIME_DAY_KEY: ("День", texts.DAY_SLOT),
    texts.TIME_NIGHT_KEY: ("Ночь", texts.NIGHT_SLOT),
}


def tariff_title(tariff_key: str | None) -> str | None:
    """Название тарифа по ключу из ``callback_data`` (``svip`` → ``S VIP``)."""
    if not tariff_key:
        return None
    return texts.TARIFF_TITLE_BY_KEY.get(tariff_key)


def time_slot_text(time_key: str | None, moment: datetime) -> str | None:
    """Строка времени для БД по выбранной кнопке.

    * ``now``     → «Сейчас (с 17:51, 23.10.2026)» — фиксируем фактический момент;
    * ``morning`` → «Утро (8:00–13:00)»;
    * ``day``     → «День (13:00–17:00)»;
    * ``night``   → «Ночь (22:00–08:00)»;
    * ``other``   → ``None`` (клиент вводит время текстом на следующем шаге).
    """
    if time_key == texts.TIME_NOW_KEY:
        return f"Сейчас (с {moment:%H:%M}, {moment:%d.%m.%Y})"
    package = _PACKAGES.get(time_key)
    if package is not None:
        label, slot = package
        return f"{label} ({slot})"
    return None


def clean_custom_time(raw: str | None) -> str | None:
    """Нормализовать введённое клиентом время; вернуть ``None``, если не подходит."""
    if not raw:
        return None
    value = " ".join(raw.split())  # схлопываем переносы строк и лишние пробелы
    if not value or value.startswith("/"):
        return None
    if len(value) > TIME_MAX_LEN:
        return None
    if not _HAS_DIGIT.search(value):
        return None
    return value


def clean_client_name(raw: str | None) -> str | None:
    """Проверить и привести имя для брони; вернуть ``None``, если ввод некорректен."""
    if not raw:
        return None
    name = " ".join(raw.split())
    if not NAME_MIN_LEN <= len(name) <= NAME_MAX_LEN:
        return None
    if name.startswith(("/", "@")):
        return None
    if not _NAME_PATTERN.fullmatch(name):
        return None
    return name[:1].upper() + name[1:]


def parse_name_and_comment(raw: str | None) -> tuple[str | None, str]:
    """Разобрать ввод гостя ``Имя`` или ``Имя // комментарий к брони``."""
    if not raw:
        return None, ""
    comment = ""
    head = raw
    for sep in ("//", "|"):
        if sep in raw:
            head, tail = raw.split(sep, 1)
            comment = " ".join(tail.split())[:160]
            break
    return clean_client_name(head), comment


def is_menu_button_text(raw: str | None) -> bool:
    """Правда, если клиент нажал кнопку reply-меню во время сценария бронирования."""
    return bool(raw) and raw.strip() in ALL_MENU_BUTTONS


__all__ = [
    "NAME_MAX_LEN",
    "NAME_MIN_LEN",
    "TIME_MAX_LEN",
    "clean_client_name",
    "clean_custom_time",
    "is_menu_button_text",
    "parse_name_and_comment",
    "tariff_title",
    "time_slot_text",
]
