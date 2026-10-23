"""Вспомогательные модули: форматирование текста и безопасные вызовы Telegram API."""

from bot.utils.formatting import (
    escape_html,
    format_datetime,
    now_full_text,
    now_text,
    plural,
    today_date_text,
    truncate,
    user_link,
)
from bot.utils.telegram import (
    edit_or_send,
    safe_delete_message,
    safe_edit_message_caption,
    safe_edit_message_text,
)

__all__ = [
    "edit_or_send",
    "escape_html",
    "format_datetime",
    "now_full_text",
    "now_text",
    "plural",
    "today_date_text",
    "truncate",
    "user_link",
    "safe_delete_message",
    "safe_edit_message_caption",
    "safe_edit_message_text",
]
