"""FSM-состояния сценария бронирования.

Порядок шагов:

1. :attr:`BookingFSM.choosing_tariff`     — выбор тарифа (inline-кнопки);
2. :attr:`BookingFSM.choosing_time`       — выбор времени (inline-кнопки);
3. :attr:`BookingFSM.entering_custom_time`— своё время текстом (если выбрано «Другое»);
4. :attr:`BookingFSM.entering_name`       — имя для брони (текст);
5. :attr:`BookingFSM.waiting_receipt`     — фото чека об оплате.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class BookingFSM(StatesGroup):
    """Состояния диалога оформления брони."""

    choosing_tariff = State()
    choosing_time = State()
    entering_custom_time = State()
    entering_name = State()
    waiting_receipt = State()


#: Ключи данных, которые кладутся в FSM-хранилище по ходу диалога.
DATA_TARIFF_KEY = "tariff_key"
DATA_TIME_KEY = "time_key"
DATA_TIME_SLOT = "time_slot"
DATA_CLIENT_NAME = "client_name"

__all__ = [
    "BookingFSM",
    "DATA_CLIENT_NAME",
    "DATA_TARIFF_KEY",
    "DATA_TIME_KEY",
    "DATA_TIME_SLOT",
]
