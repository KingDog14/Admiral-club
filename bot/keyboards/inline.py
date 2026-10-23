"""Inline-клавиатуры и формат ``callback_data``.

Все префиксы собраны здесь, чтобы обработчики не «угадывали» строки, а
использовали готовые парсеры (:func:`parse_tariff_key`, :func:`parse_time_key`,
:func:`parse_admin_decision`, :func:`parse_admin_list_page`).

Форматы ``callback_data`` (лимит Telegram — 64 байта, соблюдаются):

* ``consent:give``        — согласие на обработку ПДн (152-ФЗ);
* ``bk:t:<tariff_key>``   — выбор тарифа (standart / vip / svip / double);
* ``bk:w:<time_key>``     — выбор времени (now / night / other);
* ``bk:cancel``           — отмена бронирования;
* ``ad:stats``            — статистика админ-панели;
* ``ad:list:<page>``      — страница списка активных броней;
* ``ad:ok:<booking_id>``  — подтвердить бронь;
* ``ad:no:<booking_id>``  — отклонить бронь.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.db.models import Booking, BookingStatus
from bot.texts import TARIFFS, TIME_CHOICES, tariff_button_label

# --- идентификаторы callback_data -------------------------------------------

CB_CONSENT_GIVE: str = "consent:give"

CB_TARIFF_PREFIX: str = "bk:t:"
CB_TIME_PREFIX: str = "bk:w:"
CB_BOOKING_CANCEL: str = "bk:cancel"

CB_ADMIN_STATS: str = "ad:stats"
CB_ADMIN_LIST_PREFIX: str = "ad:list:"
CB_ADMIN_APPROVE_PREFIX: str = "ad:ok:"
CB_ADMIN_REJECT_PREFIX: str = "ad:no:"

#: Префиксы клиентского сценария бронирования (для обработки «протухших» кнопок).
BOOKING_CALLBACK_PREFIXES: tuple[str, ...] = (
    CB_TARIFF_PREFIX,
    CB_TIME_PREFIX,
    CB_BOOKING_CANCEL,
)

#: Префиксы админ-панели.
ADMIN_CALLBACK_PREFIXES: tuple[str, ...] = (
    CB_ADMIN_LIST_PREFIX,
    CB_ADMIN_APPROVE_PREFIX,
    CB_ADMIN_REJECT_PREFIX,
)

#: Сколько заявок показывается на одной странице «Активных броней».
PENDING_PAGE_SIZE: int = 5

#: Текст кнопки отмены бронирования.
CANCEL_BUTTON_TEXT: str = "❌ Отменить бронирование"


# --- генераторы callback_data ----------------------------------------------


def tariff_callback(tariff_key: str) -> str:
    return f"{CB_TARIFF_PREFIX}{tariff_key}"


def time_callback(time_key: str) -> str:
    return f"{CB_TIME_PREFIX}{time_key}"


def admin_list_callback(page: int) -> str:
    return f"{CB_ADMIN_LIST_PREFIX}{max(page, 0)}"


def admin_approve_callback(booking_id: int) -> str:
    return f"{CB_ADMIN_APPROVE_PREFIX}{booking_id}"


def admin_reject_callback(booking_id: int) -> str:
    return f"{CB_ADMIN_REJECT_PREFIX}{booking_id}"


# --- парсеры callback_data --------------------------------------------------


def parse_tariff_key(data: str | None) -> str | None:
    """Вернуть ключ тарифа из ``callback_data`` или ``None``, если ключ чужой."""
    if not data or not data.startswith(CB_TARIFF_PREFIX):
        return None
    key = data[len(CB_TARIFF_PREFIX) :]
    return key if any(tariff.key == key for tariff in TARIFFS) else None


def parse_time_key(data: str | None) -> str | None:
    """Вернуть ключ варианта времени (``now`` / ``night`` / ``other``) или ``None``."""
    if not data or not data.startswith(CB_TIME_PREFIX):
        return None
    key = data[len(CB_TIME_PREFIX) :]
    return key if any(choice_key == key for choice_key, _ in TIME_CHOICES) else None


def parse_admin_decision(data: str | None) -> tuple[str, int] | None:
    """Разобрать кнопку решения: вернуть ``(статус, id брони)`` или ``None``."""
    if not data:
        return None
    if data.startswith(CB_ADMIN_APPROVE_PREFIX):
        prefix, status = CB_ADMIN_APPROVE_PREFIX, BookingStatus.APPROVED
    elif data.startswith(CB_ADMIN_REJECT_PREFIX):
        prefix, status = CB_ADMIN_REJECT_PREFIX, BookingStatus.REJECTED
    else:
        return None
    try:
        return status, int(data[len(prefix) :])
    except ValueError:
        return None


def parse_admin_list_page(data: str | None) -> int | None:
    """Вернуть номер страницы списка заявок (0-based) или ``None``."""
    if not data or not data.startswith(CB_ADMIN_LIST_PREFIX):
        return None
    try:
        return max(int(data[len(CB_ADMIN_LIST_PREFIX) :]), 0)
    except ValueError:
        return None


# --- клавиатуры -------------------------------------------------------------


def consent_keyboard() -> InlineKeyboardMarkup:
    """Экран согласия на обработку персональных данных (152-ФЗ)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я согласен", callback_data=CB_CONSENT_GIVE)],
        ]
    )


def _cancel_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text=CANCEL_BUTTON_TEXT, callback_data=CB_BOOKING_CANCEL)


def booking_cancel_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для шагов, где клиент вводит текст (время, имя)."""
    return InlineKeyboardMarkup(inline_keyboard=[[_cancel_button()]])


def tariffs_keyboard() -> InlineKeyboardMarkup:
    """Шаг 1 бронирования: Standart, VIP, S VIP, Double."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=tariff_button_label(tariff), callback_data=tariff_callback(tariff.key))]
        for tariff in TARIFFS
    ]
    rows.append([_cancel_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def time_slots_keyboard() -> InlineKeyboardMarkup:
    """Шаг 2 бронирования: «Сейчас», «На ночь», «Другое»."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=label, callback_data=time_callback(key))]
        for key, label in TIME_CHOICES
    ]
    rows.append([_cancel_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def booking_decision_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    """Кнопки администратора под чеком: подтвердить или отклонить бронь."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить", callback_data=admin_approve_callback(booking_id)
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить", callback_data=admin_reject_callback(booking_id)
                ),
            ]
        ]
    )


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    """Панель под сообщением со статистикой (/admin)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Активные брони", callback_data=admin_list_callback(0)
                )
            ],
            [
                InlineKeyboardButton(text="🔄 Обновить статистику", callback_data=CB_ADMIN_STATS)
            ],
        ]
    )


def pending_list_keyboard(
    bookings: list[Booking],
    page: int,
    pages: int,
) -> InlineKeyboardMarkup:
    """Кнопки решения по каждой заявке + навигация по страницам списка."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"✅ №{booking.id}", callback_data=admin_approve_callback(booking.id)
            ),
            InlineKeyboardButton(
                text=f"❌ №{booking.id}", callback_data=admin_reject_callback(booking.id)
            ),
        ]
        for booking in bookings
    ]

    navigation: list[InlineKeyboardButton] = []
    if pages > 1:
        if page > 0:
            navigation.append(
                InlineKeyboardButton(text="⬅️ Назад", callback_data=admin_list_callback(page - 1))
            )
        if page < pages - 1:
            navigation.append(
                InlineKeyboardButton(text="➡️ Вперёд", callback_data=admin_list_callback(page + 1))
            )
    navigation.append(InlineKeyboardButton(text="📊 Статистика", callback_data=CB_ADMIN_STATS))
    rows.append(navigation)

    return InlineKeyboardMarkup(inline_keyboard=rows)


__all__ = [
    "ADMIN_CALLBACK_PREFIXES",
    "BOOKING_CALLBACK_PREFIXES",
    "CANCEL_BUTTON_TEXT",
    "CB_ADMIN_APPROVE_PREFIX",
    "CB_ADMIN_LIST_PREFIX",
    "CB_ADMIN_REJECT_PREFIX",
    "CB_ADMIN_STATS",
    "CB_BOOKING_CANCEL",
    "CB_CONSENT_GIVE",
    "CB_TIME_PREFIX",
    "CB_TARIFF_PREFIX",
    "PENDING_PAGE_SIZE",
    "admin_approve_callback",
    "admin_list_callback",
    "admin_panel_keyboard",
    "admin_reject_callback",
    "booking_cancel_keyboard",
    "booking_decision_keyboard",
    "consent_keyboard",
    "parse_admin_decision",
    "parse_admin_list_page",
    "parse_tariff_key",
    "parse_time_key",
    "pending_list_keyboard",
    "tariff_callback",
    "tariffs_keyboard",
    "time_callback",
    "time_slots_keyboard",
]
