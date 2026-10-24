"""Графический одноэкранный интерфейс бота клуба ADMIRAL.

* :mod:`bot.ui.cards`     — генерация HD-карточек (1280×720, Pillow) и кэш ``file_id``;
* :mod:`bot.ui.screens`   — идентификаторы экранов, ``callback_data``, клавиатуры;
* :mod:`bot.ui.render`    — сборка :class:`ScreenView` (карточка + подпись + кнопки);
* :mod:`bot.ui.navigator` — переходы через ``editMessageMedia`` и стек «◀ Назад».
"""

from bot.ui.cards import CardCache, RenderedCard, card_cache
from bot.ui.navigator import ALERT_LIMIT, STACK_LIMIT, Navigator
from bot.ui.render import CAPTION_LIMIT, MESSAGE_LIMIT, Renderer, ScreenView, ViewContext, render_screen
from bot.ui.screens import Screen, ScreenCB, nav

__all__ = [
    "ALERT_LIMIT",
    "CAPTION_LIMIT",
    "CardCache",
    "MESSAGE_LIMIT",
    "Navigator",
    "RenderedCard",
    "Renderer",
    "STACK_LIMIT",
    "Screen",
    "ScreenCB",
    "ScreenView",
    "ViewContext",
    "card_cache",
    "nav",
    "render_screen",
]
