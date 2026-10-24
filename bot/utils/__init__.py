"""Вспомогательные модули: форматирование текста и безопасные вызовы Telegram API."""

from bot.utils.formatting import (
    escape_html,
    format_datetime,
    now_full_text,
    now_text,
    plural,
    today_date_text,
    truncate,
    truncate_html,
    user_link,
    visible_length,
)
from bot.utils.telegram import (
    edit_or_send,
    safe_delete_message,
    safe_edit_message_caption,
    safe_edit_message_media,
    safe_edit_message_text,
    send_photo_screen,
    send_screen,
)

__all__ = [
    "edit_or_send",
    "escape_html",
    "format_datetime",
    "now_full_text",
    "now_text",
    "plural",
    "safe_delete_message",
    "safe_edit_message_caption",
    "safe_edit_message_media",
    "safe_edit_message_text",
    "send_photo_screen",
    "send_screen",
    "today_date_text",
    "truncate",
    "truncate_html",
    "user_link",
    "visible_length",
]
