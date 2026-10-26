"""Inline-клавиатуры и формат ``callback_data``.

Все префиксы собраны здесь, чтобы обработчики не «угадывали» строки, а
использовали готовые парсеры (:func:`parse_tariff_key`, :func:`parse_time_key`,
:func:`parse_admin_decision`, :func:`parse_admin_list_page`).

Форматы ``callback_data`` (лимит Telegram — 64 байта, соблюдаются):

* ``consent:give``        — согласие на обработку ПДн (152-ФЗ);
* ``consent:revoke``      — запрос отзыва согласия (152-ФЗ, ч. 2 ст. 9);
* ``consent:revoke:yes``  — подтвердить отзыв согласия;
* ``consent:revoke:no``   — отменить отзыв согласия;
* ``bk:t:<tariff_key>``   — выбор тарифа (standart / vip / svip / double);
* ``bk:w:<time_key>``     — выбор времени (now / morning / day / night / other);
* ``bk:pay:<way>``        — способ оплаты (``check`` — прислать чек, ``spot`` — на месте);
* ``bk:cancel``           — отмена бронирования;
* ``ad:stats``            — статистика админ-панели;
* ``ad:list:<page>``      — страница списка активных броней;
* ``ad:ok:<booking_id>``  — подтвердить бронь;
* ``ad:no:<booking_id>``  — отклонить бронь;
* ``ad:admins``           — список администраторов;
* ``ad:admin:add``        — запросить ID нового администратора;
* ``ad:admin:cancel``     — отменить добавление администратора;
* ``ad:admin:tg:<user_id>``  — включить/отключить администратора;
* ``ad:admin:up:<user_id>``  — назначить владельцем;
* ``ad:admin:dn:<user_id>``  — снять роль владельца;
* ``ad:admin:del:<user_id>`` — удалить администратора.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.db.models import Admin, AdminRole, Booking, BookingStatus
from bot.texts import TARIFFS, TIME_CHOICES, tariff_button_label

# --- идентификаторы callback_data -------------------------------------------

CB_CONSENT_GIVE: str = "consent:give"
CB_CONSENT_REVOKE: str = "consent:revoke"
CB_CONSENT_REVOKE_YES: str = "consent:revoke:yes"
CB_CONSENT_REVOKE_NO: str = "consent:revoke:no"

#: Все callback-и, связанные с согласием на обработку ПДн.
CONSENT_CALLBACKS: tuple[str, ...] = (
    CB_CONSENT_GIVE,
    CB_CONSENT_REVOKE,
    CB_CONSENT_REVOKE_YES,
    CB_CONSENT_REVOKE_NO,
)

CB_TARIFF_PREFIX: str = "bk:t:"
CB_TIME_PREFIX: str = "bk:w:"
CB_PAY_PREFIX: str = "bk:pay:"
CB_PAY_CHECK: str = "bk:pay:check"
CB_PAY_SPOT: str = "bk:pay:spot"
CB_BOOKING_CANCEL: str = "bk:cancel"
#: Шаг назад внутри мастера бронирования.
CB_BOOKING_BACK: str = "bk:back"

#: Способы оплаты на шаге 4.
PAY_WAY_CHECK: str = "check"
PAY_WAY_SPOT: str = "spot"

CB_ADMIN_STATS: str = "ad:stats"
CB_ADMIN_LIST_PREFIX: str = "ad:list:"
CB_ADMIN_APPROVE_PREFIX: str = "ad:ok:"
CB_ADMIN_REJECT_PREFIX: str = "ad:no:"

# Управление администраторами (доступно владельцу).
CB_ADMIN_ADMINS: str = "ad:admins"
CB_ADMIN_ADD: str = "ad:admin:add"
CB_ADMIN_ADD_CANCEL: str = "ad:admin:cancel"
CB_ADMIN_TOGGLE_PREFIX: str = "ad:admin:tg:"
CB_ADMIN_PROMOTE_PREFIX: str = "ad:admin:up:"
CB_ADMIN_DEMOTE_PREFIX: str = "ad:admin:dn:"
CB_ADMIN_DELETE_PREFIX: str = "ad:admin:del:"

#: Префиксы клиентского сценария бронирования (для обработки «протухших» кнопок).
BOOKING_CALLBACK_PREFIXES: tuple[str, ...] = (
    CB_TARIFF_PREFIX,
    CB_TIME_PREFIX,
    CB_PAY_PREFIX,
    CB_BOOKING_BACK,
    CB_BOOKING_CANCEL,
)

#: Префиксы админ-панели.
ADMIN_CALLBACK_PREFIXES: tuple[str, ...] = (
    CB_ADMIN_LIST_PREFIX,
    CB_ADMIN_APPROVE_PREFIX,
    CB_ADMIN_REJECT_PREFIX,
    CB_ADMIN_TOGGLE_PREFIX,
    CB_ADMIN_PROMOTE_PREFIX,
    CB_ADMIN_DEMOTE_PREFIX,
    CB_ADMIN_DELETE_PREFIX,
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


def admin_toggle_callback(user_id: int) -> str:
    return f"{CB_ADMIN_TOGGLE_PREFIX}{user_id}"


def admin_promote_callback(user_id: int) -> str:
    return f"{CB_ADMIN_PROMOTE_PREFIX}{user_id}"


def admin_demote_callback(user_id: int) -> str:
    return f"{CB_ADMIN_DEMOTE_PREFIX}{user_id}"


def admin_delete_callback(user_id: int) -> str:
    return f"{CB_ADMIN_DELETE_PREFIX}{user_id}"


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


def parse_payment_way(data: str | None) -> str | None:
    """Вернуть способ оплаты из ``callback_data`` (``check`` / ``spot``) или ``None``."""
    if not data or not data.startswith(CB_PAY_PREFIX):
        return None
    way = data[len(CB_PAY_PREFIX) :]
    return way if way in (PAY_WAY_CHECK, PAY_WAY_SPOT) else None


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


def _parse_admin_target(data: str | None, prefix: str) -> int | None:
    """Общий разбор ``callback_data`` с числовым ID администратора."""
    if not data or not data.startswith(prefix):
        return None
    try:
        value = int(data[len(prefix) :])
    except ValueError:
        return None
    return value if value > 0 else None


def parse_admin_toggle(data: str | None) -> int | None:
    """ID администратора из кнопки «включить/отключить»."""
    return _parse_admin_target(data, CB_ADMIN_TOGGLE_PREFIX)


def parse_admin_delete(data: str | None) -> int | None:
    """ID администратора из кнопки «удалить»."""
    return _parse_admin_target(data, CB_ADMIN_DELETE_PREFIX)


def parse_admin_role(data: str | None) -> tuple[str, int] | None:
    """Разобрать кнопку смены роли: вернуть ``(новая роль, user_id)``."""
    promote = _parse_admin_target(data, CB_ADMIN_PROMOTE_PREFIX)
    if promote is not None:
        return AdminRole.OWNER, promote
    demote = _parse_admin_target(data, CB_ADMIN_DEMOTE_PREFIX)
    if demote is not None:
        return AdminRole.OPERATOR, demote
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


_SUPPORTS_STYLE: bool = "style" in InlineKeyboardButton.model_fields


def _btn(
    text: str, callback_data: str, *, style: str | None = None
) -> InlineKeyboardButton:
    """Собрать кнопку с нативным цветовым акцентом Bot API (если поддерживается)."""
    if style and _SUPPORTS_STYLE:
        return InlineKeyboardButton(text=text, callback_data=callback_data, style=style)
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def consent_keyboard() -> InlineKeyboardMarkup:
    """Экран согласия на обработку персональных данных (152-ФЗ)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("✅ Я согласен", CB_CONSENT_GIVE, style="success")],
        ]
    )


def privacy_keyboard() -> InlineKeyboardMarkup:
    """Раздел /privacy: кнопка отзыва согласия на обработку ПДн."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🚫 Отозвать согласие", CB_CONSENT_REVOKE, style="danger")],
        ]
    )


def revoke_confirm_keyboard() -> InlineKeyboardMarkup:
    """Подтверждение отзыва согласия: бот перестанет работать для пользователя."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn("✅ Да, отозвать", CB_CONSENT_REVOKE_YES, style="danger"),
                _btn("↩️ Отмена", CB_CONSENT_REVOKE_NO, style="success"),
            ],
        ]
    )


def payment_choice_keyboard(prepayment_required: bool) -> InlineKeyboardMarkup:
    """Шаг 4: как оплатить — прислать чек или заплатить на месте."""
    rows: list[list[InlineKeyboardButton]] = [
        [_btn("📸 Оплатил — отправить чек", CB_PAY_CHECK, style="success")],
    ]
    if not prepayment_required:
        rows.append([_btn("💵 Оплачу на месте", CB_PAY_SPOT, style="primary")])
    rows.append([_cancel_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cancel_button() -> InlineKeyboardButton:
    return _btn(CANCEL_BUTTON_TEXT, CB_BOOKING_CANCEL, style="danger")


def booking_cancel_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для шагов, где клиент вводит текст (время, имя)."""
    return InlineKeyboardMarkup(inline_keyboard=[[_cancel_button()]])


_TARIFF_STYLES: dict[str, str] = {
    "standart": "success",
    "vip": "primary",
    "svip": "danger",
    "double": "primary",
}

_TIME_STYLES: dict[str, str] = {
    "now": "success",
    "morning": "primary",
    "day": "primary",
    "night": "success",
    "other": "primary",
}


def tariffs_keyboard() -> InlineKeyboardMarkup:
    """Шаг 1 бронирования: Standart, VIP, S VIP, Double."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            _btn(
                tariff_button_label(tariff),
                tariff_callback(tariff.key),
                style=_TARIFF_STYLES.get(tariff.key, "primary"),
            )
        ]
        for tariff in TARIFFS
    ]
    rows.append([_cancel_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def time_slots_keyboard() -> InlineKeyboardMarkup:
    """Шаг 2 бронирования: «Сейчас», «На ночь», «Другое»."""
    rows: list[list[InlineKeyboardButton]] = [
        [_btn(label, time_callback(key), style=_TIME_STYLES.get(key, "primary"))]
        for key, label in TIME_CHOICES
    ]
    rows.append([_cancel_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def booking_decision_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    """Кнопки администратора под чеком: подтвердить или отклонить бронь."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn(
                    "✅ Подтвердить",
                    admin_approve_callback(booking_id),
                    style="success",
                ),
                _btn("❌ Отклонить", admin_reject_callback(booking_id), style="danger"),
            ]
        ]
    )


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    """Панель под сообщением со статистикой (/admin)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("📋 Активные брони", admin_list_callback(0), style="success")],
            [_btn("👥 Администраторы", CB_ADMIN_ADMINS, style="primary")],
            [_btn("🔄 Обновить статистику", CB_ADMIN_STATS, style="primary")],
        ]
    )


def admin_add_cancel_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура под запросом ID нового администратора."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn("❌ Отменить", CB_ADMIN_ADD_CANCEL, style="danger")]]
    )


def admins_nav_keyboard() -> InlineKeyboardMarkup:
    """Навигация под пустым/служебным сообщением раздела администраторов."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn("➕ Добавить", CB_ADMIN_ADD, style="success"),
                _btn("🔄 Обновить", CB_ADMIN_ADMINS, style="primary"),
            ],
            [_btn("📊 Статистика", CB_ADMIN_STATS, style="primary")],
        ]
    )


def admins_list_keyboard(
    admins: list[Admin],
    *,
    env_owner_id: int,
    viewer_is_owner: bool,
) -> InlineKeyboardMarkup:
    """Кнопки управления администраторами.

    * владельцу доступны включение/отключение, смена роли и удаление;
    * владелец из ``.env`` защищён — под ним кнопок нет;
    * обычному администратору показываем только навигацию (управление запрещено).
    """
    if not viewer_is_owner:
        return InlineKeyboardMarkup(
            inline_keyboard=[[_btn("📊 Статистика", CB_ADMIN_STATS, style="primary")]]
        )

    rows: list[list[InlineKeyboardButton]] = []
    for admin in admins:
        if admin.user_id == env_owner_id:
            continue  # защищённый владелец из .env — кнопок не даём
        rows.append(
            [
                _btn(
                    "✅ Включить" if not admin.is_active else "⛔️ Отключить",
                    admin_toggle_callback(admin.user_id),
                    style="success" if not admin.is_active else "danger",
                ),
                _btn(
                    "👤 Оператор" if admin.is_owner else "👑 Владелец",
                    admin_demote_callback(admin.user_id)
                    if admin.is_owner
                    else admin_promote_callback(admin.user_id),
                    style="primary",
                ),
                _btn("🗑 Удалить", admin_delete_callback(admin.user_id), style="danger"),
            ]
        )

    rows.append(
        [
            _btn("➕ Добавить", CB_ADMIN_ADD, style="success"),
            _btn("🔄 Обновить", CB_ADMIN_ADMINS, style="primary"),
        ]
    )
    rows.append([_btn("📊 Статистика", CB_ADMIN_STATS, style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pending_list_keyboard(
    bookings: list[Booking],
    page: int,
    pages: int,
) -> InlineKeyboardMarkup:
    """Кнопки решения по каждой заявке + навигация по страницам списка."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            _btn(
                f"✅ №{booking.id}", admin_approve_callback(booking.id), style="success"
            ),
            _btn(
                f"❌ №{booking.id}", admin_reject_callback(booking.id), style="danger"
            ),
        ]
        for booking in bookings
    ]

    navigation: list[InlineKeyboardButton] = []
    if pages > 1:
        if page > 0:
            navigation.append(
                _btn("⬅️ Назад", admin_list_callback(page - 1), style="primary")
            )
        if page < pages - 1:
            navigation.append(
                _btn("➡️ Вперёд", admin_list_callback(page + 1), style="primary")
            )
    navigation.append(_btn("📊 Статистика", CB_ADMIN_STATS, style="primary"))
    rows.append(navigation)

    return InlineKeyboardMarkup(inline_keyboard=rows)


__all__ = [
    "ADMIN_CALLBACK_PREFIXES",
    "BOOKING_CALLBACK_PREFIXES",
    "CANCEL_BUTTON_TEXT",
    "CB_ADMIN_ADD",
    "CB_ADMIN_ADD_CANCEL",
    "CB_ADMIN_ADMINS",
    "CB_ADMIN_APPROVE_PREFIX",
    "CB_ADMIN_DELETE_PREFIX",
    "CB_ADMIN_DEMOTE_PREFIX",
    "CB_ADMIN_LIST_PREFIX",
    "CB_ADMIN_PROMOTE_PREFIX",
    "CB_ADMIN_REJECT_PREFIX",
    "CB_ADMIN_STATS",
    "CB_ADMIN_TOGGLE_PREFIX",
    "CB_BOOKING_BACK",
    "CB_BOOKING_CANCEL",
    "CB_CONSENT_GIVE",
    "CB_CONSENT_REVOKE",
    "CB_CONSENT_REVOKE_NO",
    "CB_CONSENT_REVOKE_YES",
    "CB_PAY_CHECK",
    "CB_PAY_PREFIX",
    "CB_PAY_SPOT",
    "CB_TARIFF_PREFIX",
    "CB_TIME_PREFIX",
    "CONSENT_CALLBACKS",
    "PAY_WAY_CHECK",
    "PAY_WAY_SPOT",
    "PENDING_PAGE_SIZE",
    "admin_add_cancel_keyboard",
    "admin_approve_callback",
    "admin_delete_callback",
    "admin_demote_callback",
    "admin_list_callback",
    "admin_panel_keyboard",
    "admin_promote_callback",
    "admin_reject_callback",
    "admin_toggle_callback",
    "admins_list_keyboard",
    "admins_nav_keyboard",
    "booking_cancel_keyboard",
    "booking_decision_keyboard",
    "consent_keyboard",
    "parse_admin_decision",
    "parse_admin_delete",
    "parse_admin_list_page",
    "parse_admin_role",
    "parse_admin_toggle",
    "parse_payment_way",
    "parse_tariff_key",
    "parse_time_key",
    "payment_choice_keyboard",
    "pending_list_keyboard",
    "privacy_keyboard",
    "revoke_confirm_keyboard",
    "tariff_callback",
    "tariffs_keyboard",
    "time_callback",
    "time_slots_keyboard",
]
