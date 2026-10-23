"""Интерфейс бота: экраны, навигация и их отрисовка.

Бот работает как меню игрового клуба, а не как переписка:

* у пользователя всегда **ровно один** экран — сообщение перезаписывается,
  а не дублируется (``editMessageText``);
* навигация — только inline-кнопки под этим сообщением;
* «◀ Назад» ведёт на предыдущий экран (стек переходов), «🏠 В меню» — домой;
* шапка экрана — моноширинная рамка с названием клуба.

Модули:

* :mod:`bot.ui.screens`  — идентификаторы экранов, ``callback_data``, рамки,
  таблицы, клавиатуры;
* :mod:`bot.ui.render`   — содержимое экранов (текст + клавиатура) по базе и
  FSM-данным;
* :mod:`bot.ui.navigator` — переходы, стек «Назад» и правка сообщения-экрана.
"""

from bot.ui.navigator import ALERT_LIMIT, STACK_LIMIT, Navigator
from bot.ui.render import MESSAGE_LIMIT, Renderer, ViewContext, render_screen
from bot.ui.screens import Screen, ScreenCB, nav

__all__ = [
    "ALERT_LIMIT",
    "MESSAGE_LIMIT",
    "STACK_LIMIT",
    "Navigator",
    "Renderer",
    "Screen",
    "ScreenCB",
    "ViewContext",
    "nav",
    "render_screen",
]
