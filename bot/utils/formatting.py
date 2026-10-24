"""Форматирование текста для сообщений бота (parse_mode=HTML).

Все значения, которые пришли от пользователей (имена, username, произвольный
текст времени), перед подстановкой в HTML-шаблон обязательно экранируются —
иначе сообщение либо не отправится, либо разметка «поедет».
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

MONTHS_GENITIVE: tuple[str, ...] = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

_TAG_RE = re.compile(r"<(/?)([a-zA-Z0-9]+)(?:\s[^>]*)?>")
_TOKEN_RE = re.compile(r"(<[^>]+>|&[a-zA-Z0-9#]+;|.)")


def escape_html(value: object) -> str:
    """Экранировать ``<``, ``>``, ``&`` для HTML-разметки Telegram."""
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def visible_length(html_text: str) -> int:
    """Видимая длина HTML-текста без тегов (как считает лимит подписи Telegram)."""
    stripped = re.sub(r"<[^>]+>", "", html_text)
    return len(html.unescape(stripped))


def truncate(text: str, limit: int) -> str:
    """Обрезать обычный текст до ``limit`` символов, добавив многоточие."""
    if limit <= 1:
        return text[:limit]
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


def truncate_html(html_text: str, limit: int) -> str:
    """Безопасно уложить HTML-подпись в лимит Telegram, не ломая открытые теги."""
    if limit <= 1:
        return ""
    if visible_length(html_text) <= limit:
        return html_text

    budget = limit - 1
    out: list[str] = []
    open_tags: list[str] = []
    used = 0

    for match in _TOKEN_RE.finditer(html_text):
        tok = match.group(0)
        if tok.startswith("<") and tok.endswith(">"):
            tag_m = _TAG_RE.match(tok)
            if tag_m:
                closing, name = tag_m.group(1), tag_m.group(2).lower()
                if closing:
                    if name in open_tags:
                        while open_tags:
                            popped = open_tags.pop()
                            out.append(f"</{popped}>")
                            if popped == name:
                                break
                    continue
                if not tok.endswith("/>") and name != "br":
                    open_tags.append(name)
            out.append(tok)
            continue

        char_len = len(html.unescape(tok))
        if used + char_len > budget:
            break
        out.append(tok)
        used += char_len

    result = "".join(out).rstrip() + "…"
    for tag in reversed(open_tags):
        result += f"</{tag}>"
    return result


def parse_iso_datetime(value: str | datetime | None) -> datetime | None:
    """Разобрать строку даты из БД (ISO-8601) в :class:`datetime`."""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    text = str(value).strip()
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def format_datetime(
    value: str | datetime | None,
    tz: ZoneInfo | None = None,
    fmt: str = "%d.%m.%Y %H:%M",
) -> str:
    """Отформатировать дату из БД в понятный администратору вид."""
    moment = parse_iso_datetime(value)
    if moment is None:
        return "—"
    if tz is not None:
        moment = moment.astimezone(tz)
    return moment.strftime(fmt)


def now_text(tz: ZoneInfo) -> str:
    """Текущее время клуба: «17:51»."""
    return datetime.now(tz).strftime("%H:%M")


def today_date_text(tz: ZoneInfo) -> str:
    """Текущая дата клуба по-русски: «23 октября 2026, пятница»."""
    moment = datetime.now(tz)
    weekday = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")[
        moment.weekday()
    ]
    return f"{moment.day} {MONTHS_GENITIVE[moment.month - 1]} {moment.year}, {weekday}"


def plural(count: int, one: str, few: str, many: str) -> str:
    """Согласовать существительное с числом: 1 заявка, 2 заявки, 5 заявок."""
    mod10, mod100 = count % 10, count % 100
    if mod10 == 1 and mod100 != 11:
        return one
    if 2 <= mod10 <= 4 and not 12 <= mod100 <= 14:
        return few
    return many


def user_link(user_id: int, username: str | None = None, first_name: str | None = None) -> str:
    """HTML-ссылка на пользователя для админ-панели."""
    if username:
        return f'<a href="https://t.me/{escape_html(username)}">@{escape_html(username)}</a>'
    name = escape_html(first_name) if first_name else "без имени"
    return f"{name} (ID: <code>{user_id}</code>)"


def now_full_text(tz: ZoneInfo) -> str:
    """Текущие дата и время клуба: «23.10.2026 17:51»."""
    return datetime.now(tz).strftime("%d.%m.%Y %H:%M")
