"""Reply-клавиатура главного меню клиента.

Тексты кнопок одновременно являются фильтрами в ``bot/handlers/client.py``,
поменять подпись кнопки можно только вместе с константой в обработчике
(константы импортируются оттуда же, так что достаточно править этот файл).
"""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

MENU_PRICE: str = "💰 Прайс-лист"
MENU_RULES: str = "📜 Правила"
MENU_BOOKING: str = "🎮 Забронировать"
MENU_FAQ: str = "❓ Частые вопросы"
MENU_CONTACTS: str = "📞 Связаться с админом"

MENU_PLACEHOLDER: str = "Выберите действие…"

#: Все кнопки меню — нужны, чтобы отличать нажатие меню от ввода текста в FSM.
ALL_MENU_BUTTONS: frozenset[str] = frozenset(
    {MENU_PRICE, MENU_RULES, MENU_BOOKING, MENU_FAQ, MENU_CONTACTS}
)


def main_menu() -> ReplyKeyboardMarkup:
    """Главное меню: прайс, правила, бронирование, вопросы, связь с админом."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=MENU_PRICE),
                KeyboardButton(text=MENU_RULES),
            ],
            [
                KeyboardButton(text=MENU_BOOKING),
                KeyboardButton(text=MENU_FAQ),
            ],
            [
                KeyboardButton(text=MENU_CONTACTS),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder=MENU_PLACEHOLDER,
        is_persistent=True,
    )


def remove_keyboard() -> ReplyKeyboardRemove:
    """Убрать reply-меню на время сценария бронирования."""
    return ReplyKeyboardRemove()


__all__ = [
    "ALL_MENU_BUTTONS",
    "MENU_BOOKING",
    "MENU_CONTACTS",
    "MENU_FAQ",
    "MENU_PRICE",
    "MENU_RULES",
    "main_menu",
    "remove_keyboard",
]
