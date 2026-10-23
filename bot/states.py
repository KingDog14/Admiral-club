"""Состояния конечных автоматов (FSM).

Клиентские состояния — бронирование: тариф → время → имя → способ оплаты →
чек об оплате (если клиент выбрал оплату переводом).
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
    #: Ожидание выбора способа оплаты: прислать чек или оплатить на месте.
    choosing_payment = State()
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

# --- навигация по экранам (см. bot/ui/navigator.py) -------------------------- #
#: Текущий экран: {"s": <screen>, "a": <arg>}.
DATA_SCREEN: str = "screen"
#: Стек переходов для кнопки «◀ Назад»: список таких же записей.
DATA_SCREEN_STACK: str = "screen_stack"
#: ``message_id`` сообщения, которое сейчас является экраном пользователя.
DATA_SCREEN_MESSAGE: str = "screen_message"

__all__ = [
    "AdminFSM",
    "BookingFSM",
    "DATA_ADMIN_ID",
    "DATA_SCREEN",
    "DATA_SCREEN_MESSAGE",
    "DATA_SCREEN_STACK",
    "DATA_CLIENT_NAME",
    "DATA_TARIFF_KEY",
    "DATA_TIME_KEY",
    "DATA_TIME_SLOT",
]
