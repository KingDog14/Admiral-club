"""Состояния конечных автоматов (FSM).

Клиентские состояния — бронирование: тариф → время → имя → чек об оплате.
Администраторские состояния — добавление нового администратора по ID.
"""

from aiogram.fsm.state import State, StatesGroup


class BookingFSM(StatesGroup):
    """Пошаговое бронирование компьютера в клубе."""

    #: Ожидание выбора тарифа по inline-кнопке.
    choosing_tariff = State()
    #: Ожидание выбора времени по inline-кнопке.
    choosing_time = State()
    #: Ожидание произвольного времени текстом (после кнопки «Другое»).
    entering_custom_time = State()
    #: Ожидание имени клиента текстом.
    entering_name = State()
    #: Ожидание ФОТО чека об оплате.
    waiting_receipt = State()


class AdminFSM(StatesGroup):
    """Действия администратора в панели управления."""

    #: Ожидание числового Telegram ID нового администратора.
    waiting_admin_id = State()


#: Ключи FSM-данных клиента (сохраняются между шагами бронирования).
DATA_TARIFF_KEY: str = "tariff_key"
DATA_TIME_KEY: str = "time_key"
DATA_TIME_SLOT: str = "time_slot"
DATA_CLIENT_NAME: str = "client_name"

#: Ключ FSM-данных администратора (не используется как значение, только ключ).
DATA_ADMIN_ID: str = "admin_id"

__all__ = [
    "AdminFSM",
    "BookingFSM",
    "DATA_ADMIN_ID",
    "DATA_CLIENT_NAME",
    "DATA_TARIFF_KEY",
    "DATA_TIME_KEY",
    "DATA_TIME_SLOT",
]
