"""Состояния конечных автоматов (FSM).

Клиентские состояния — бронирование: точка (если в сети 2+) → зал → время →
имя / гости / доп. услуги → способ оплаты → чек об оплате (фото или PDF).
Администраторские состояния — добавление администратора, отклонение/отмена с
комментарием, правка брони, ручная бронь за гостя, настройка расписания,
тарифов, точек сети, тихих часов и параметров клуба.
"""

from aiogram.fsm.state import State, StatesGroup


class BookingFSM(StatesGroup):
    """Пошаговое бронирование компьютера в клубе."""

    #: Выбор точки клуба (активно, когда в сети 2 и более точек).
    choosing_branch = State()
    #: Ожидание выбора тарифа по inline-кнопке.
    choosing_tariff = State()
    #: Ожидание выбора времени по inline-кнопке.
    choosing_time = State()
    #: Ожидание произвольного времени текстом (после кнопки «Другое»).
    entering_custom_time = State()
    #: Ожидание имени клиента (и при желании комментария) текстом.
    entering_name = State()
    #: Ожидание выбора способа оплаты: прислать чек или оплатить на месте.
    choosing_payment = State()
    #: Ожидание ФОТО или PDF чека об оплате.
    waiting_receipt = State()


class AdminFSM(StatesGroup):
    """Действия администратора в панели управления."""

    #: Ожидание «Имя + ID» или числового Telegram ID нового администратора.
    waiting_admin_id = State()
    #: Ожидание опционального комментария при отклонении новой брони.
    entering_reject_comment = State()
    #: Ожидание опционального комментария при отмене подтверждённой брони.
    entering_cancel_comment = State()
    #: Ожидание нового значения поля при изменении существующей брони.
    editing_booking_field = State()
    #: Ожидание текстового ввода времени при ручном создании брони.
    manual_entering_time = State()
    #: Ожидание данных гостя («Имя | контакт | ПК») при ручном создании брони.
    manual_entering_guest = State()
    #: Ожидание ввода параметров расписания / праздников / занятых слотов.
    editing_schedule = State()
    #: Ожидание ввода цен тарифа или новой доп. услуги.
    editing_tariff = State()
    #: Ожидание ввода данных новой или редактируемой точки сети.
    editing_branch = State()
    #: Ожидание интервала тихих часов уведомлений («23:00-08:00»).
    editing_quiet_hours = State()
    #: Ожидание текста или фото для настройки клуба (реквизиты, приветствие, чат, правила).
    editing_setting = State()


#: Ключи FSM-данных клиента.
DATA_BRANCH_ID: str = "branch_id"
DATA_TARIFF_KEY: str = "tariff_key"
DATA_TIME_KEY: str = "time_key"
DATA_TIME_SLOT: str = "time_slot"
DATA_BOOKING_DATE: str = "booking_date"
DATA_SLOT_START: str = "slot_start"
DATA_SLOT_END: str = "slot_end"
DATA_CLIENT_NAME: str = "client_name"
DATA_GUESTS_COUNT: str = "guests_count"
DATA_SEAT_LABEL: str = "seat_label"
DATA_GUEST_COMMENT: str = "guest_comment"
DATA_EXTRAS: str = "extras"

#: Ключи FSM-данных администратора.
DATA_ADMIN_ID: str = "admin_id"
DATA_ADMIN_TARGET_BOOKING: str = "adm_booking_id"
DATA_ADMIN_REJECT_REASON: str = "adm_reject_reason"
DATA_ADMIN_EDIT_FIELD: str = "adm_edit_field"
DATA_ADMIN_MANUAL: str = "adm_manual"
DATA_ADMIN_SCHED_TARGET: str = "adm_sched_target"
DATA_ADMIN_TARIFF_TARGET: str = "adm_tariff_target"
DATA_ADMIN_TARGET_BRANCH: str = "adm_branch_id"
DATA_ADMIN_SETTING_KEY: str = "adm_setting_key"

# --- навигация по экранам (см. bot/ui/navigator.py) -------------------------- #
DATA_SCREEN: str = "screen"
DATA_SCREEN_STACK: str = "screen_stack"
DATA_SCREEN_MESSAGE: str = "screen_message"

__all__ = [
    "DATA_ADMIN_EDIT_FIELD",
    "DATA_ADMIN_ID",
    "DATA_ADMIN_MANUAL",
    "DATA_ADMIN_REJECT_REASON",
    "DATA_ADMIN_SCHED_TARGET",
    "DATA_ADMIN_SETTING_KEY",
    "DATA_ADMIN_TARGET_BOOKING",
    "DATA_ADMIN_TARGET_BRANCH",
    "DATA_ADMIN_TARIFF_TARGET",
    "DATA_BOOKING_DATE",
    "DATA_BRANCH_ID",
    "DATA_CLIENT_NAME",
    "DATA_EXTRAS",
    "DATA_GUESTS_COUNT",
    "DATA_GUEST_COMMENT",
    "DATA_SCREEN",
    "DATA_SCREEN_MESSAGE",
    "DATA_SCREEN_STACK",
    "DATA_SEAT_LABEL",
    "DATA_SLOT_END",
    "DATA_SLOT_START",
    "DATA_TARIFF_KEY",
    "DATA_TIME_KEY",
    "DATA_TIME_SLOT",
    "AdminFSM",
    "BookingFSM",
]
