"""Экраны интерфейса: один экран — одно сообщение, которое бот перезаписывает.

Интерфейс бота собран как меню игрового клуба, а не как переписка:

* у пользователя всегда **ровно один** актуальный экран — бот не пишет новое
  сообщение, а редактирует текущее (``editMessageText``);
* вся навигация — inline-кнопки под этим же сообщением, reply-клавиатуры нет;
* у каждого экрана есть «◀ Назад» и «🏠 В меню», а переходы запоминаются в
  стеке, поэтому «Назад» возвращает туда, откуда пользователь пришёл;
* шапка каждого экрана — моноширинная рамка (HUD) с названием клуба, как на
  табло в компьютерном клубе.

Здесь лежат только «чистые» вещи: идентификаторы экранов, ``callback_data``,
рисование рамок и сборка клавиатур. За содержимое экранов (тексты, запросы к
базе) отвечает :mod:`bot.ui.render`, а за переходы — :mod:`bot.ui.navigator`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot import texts
from bot.db.models import BookingStatus
from bot.utils.formatting import escape_html


# --------------------------------------------------------------------------- #
# Идентификаторы экранов
# --------------------------------------------------------------------------- #
class Screen:
    """Экраны бота. Значение — часть ``callback_data``, поэтому короткое."""

    #: Главное меню.
    MENU: Final[str] = "menu"
    #: Залы и цены.
    PRICES: Final[str] = "prices"
    #: Правила клуба.
    RULES: Final[str] = "rules"
    #: Список вопросов FAQ.
    FAQ: Final[str] = "faq"
    #: Ответ на один вопрос FAQ (в ``arg`` — ключ вопроса).
    FAQ_ITEM: Final[str] = "faq_item"
    #: Контакты и реквизиты.
    CONTACTS: Final[str] = "contacts"
    #: Брони текущего клиента.
    BOOKINGS: Final[str] = "bookings"

    #: Бронирование, шаг 1 — выбор зала.
    BOOK_TARIFF: Final[str] = "book_tariff"
    #: Бронирование, шаг 2 — выбор времени.
    BOOK_TIME: Final[str] = "book_time"
    #: Бронирование, шаг 2 — ввод своего времени текстом.
    BOOK_TIME_TEXT: Final[str] = "book_time_text"
    #: Бронирование, шаг 3 — ввод имени.
    BOOK_NAME: Final[str] = "book_name"
    #: Бронирование, шаг 4 — выбор способа оплаты.
    BOOK_PAY: Final[str] = "book_pay"
    #: Бронирование, шаг 4 — ожидание фото чека.
    BOOK_RECEIPT: Final[str] = "book_receipt"
    #: Заявка принята (в ``arg`` — номер заявки).
    BOOK_DONE: Final[str] = "book_done"

    #: Админ-панель: сводка.
    ADMIN: Final[str] = "admin"
    #: Активные брони (в ``arg`` — номер страницы).
    ADMIN_PENDING: Final[str] = "admin_pending"
    #: Список администраторов.
    ADMIN_ADMINS: Final[str] = "admin_admins"
    #: Карточка администратора (в ``arg`` — Telegram ID).
    ADMIN_ADMIN: Final[str] = "admin_admin"
    #: Добавление администратора (ввод ID).
    ADMIN_ADD: Final[str] = "admin_add"

    #: Персональные данные (152-ФЗ) и отзыв согласия.
    PRIVACY: Final[str] = "privacy"


#: Заголовки экранов — подставляются в шапку HUD.
SCREEN_TITLES: Final[dict[str, str]] = {
    Screen.MENU: "ГЛАВНОЕ МЕНЮ",
    Screen.PRICES: "ЗАЛЫ И ЦЕНЫ",
    Screen.RULES: "ПРАВИЛА КЛУБА",
    Screen.FAQ: "ЧАСТЫЕ ВОПРОСЫ",
    Screen.FAQ_ITEM: "ЧАСТЫЕ ВОПРОСЫ",
    Screen.CONTACTS: "КОНТАКТЫ",
    Screen.BOOKINGS: "МОИ БРОНИ",
    Screen.BOOK_TARIFF: "БРОНЬ · ШАГ 1/4 · ЗАЛ",
    Screen.BOOK_TIME: "БРОНЬ · ШАГ 2/4 · ВРЕМЯ",
    Screen.BOOK_TIME_TEXT: "БРОНЬ · ШАГ 2/4 · ВРЕМЯ",
    Screen.BOOK_NAME: "БРОНЬ · ШАГ 3/4 · ИМЯ",
    Screen.BOOK_PAY: "БРОНЬ · ШАГ 4/4 · ОПЛАТА",
    Screen.BOOK_RECEIPT: "БРОНЬ · ШАГ 4/4 · ЧЕК",
    Screen.BOOK_DONE: "ЗАЯВКА ОТПРАВЛЕНА",
    Screen.ADMIN: "АДМИН-ПАНЕЛЬ",
    Screen.ADMIN_PENDING: "АКТИВНЫЕ БРОНИ",
    Screen.ADMIN_ADMINS: "АДМИНИСТРАТОРЫ",
    Screen.ADMIN_ADMIN: "АДМИНИСТРАТОР",
    Screen.ADMIN_ADD: "НОВЫЙ АДМИНИСТРАТОР",
    Screen.PRIVACY: "ПЕРСОНАЛЬНЫЕ ДАННЫЕ",
}

#: Экраны-шаги бронирования: на них «Назад» ведёт на предыдущий шаг.
BOOKING_SCREENS: Final[frozenset[str]] = frozenset(
    {
        Screen.BOOK_TARIFF,
        Screen.BOOK_TIME,
        Screen.BOOK_TIME_TEXT,
        Screen.BOOK_NAME,
        Screen.BOOK_PAY,
        Screen.BOOK_RECEIPT,
    }
)


class ScreenCB(CallbackData, prefix="nav"):
    """``callback_data`` перехода между экранами: ``nav:<screen>:<arg>``."""

    screen: str = Screen.MENU
    arg: str = ""


def nav(screen: str, arg: str | int = "") -> str:
    """Собрать ``callback_data`` кнопки-перехода на экран."""
    return ScreenCB(screen=screen, arg=str(arg)).pack()


# --------------------------------------------------------------------------- #
# Оформление: моноширинная шапка, таблицы, подписи кнопок
# --------------------------------------------------------------------------- #
#: Ширина рамки HUD в символах (считая внутренние отступы).
HUD_WIDTH: Final[int] = 28

#: Рамка «табло клуба»: ┌────┐ / │ ▚▚ ADMIRAL ▚▚ │ / └────┘
HUD_BORDER_TOP: Final[str] = "┌" + "─" * HUD_WIDTH + "┐"
HUD_BORDER_BOTTOM: Final[str] = "└" + "─" * HUD_WIDTH + "┘"

#: Подписи служебных кнопок (одинаковые на всех экранах).
BTN_BACK: Final[str] = "◀ Назад"
BTN_HOME: Final[str] = "🏠 В меню"
BTN_REFRESH: Final[str] = "🔄 Обновить"
BTN_STEP_BACK: Final[str] = "‹ Шаг назад"
BTN_CANCEL: Final[str] = "✕ Отменить"


def _hud_row(text: str) -> str:
    """Строка рамки с текстом по центру."""
    pad = HUD_WIDTH - len(text)
    if pad <= 0:
        return f"│{text[:HUD_WIDTH]}│"
    left = pad // 2
    return f"│{' ' * left}{text}{' ' * (pad - left)}│"


def hud(title: str, subtitle: str = "") -> str:
    """Моноширинная шапка экрана — «табло» над содержимым.

    Пример::

        ┌────────────────────────────┐
        │       ▚▚ ADMIRAL ▚▚        │
        │    ЧИТА · КРУГЛОСУТОЧНО    │
        └────────────────────────────┘
    """
    rows = [_hud_row(title)]
    if subtitle:
        rows.append(_hud_row(subtitle))
    body = "\n".join([HUD_BORDER_TOP, *rows, HUD_BORDER_BOTTOM])
    return f"<pre>{escape_html(body)}</pre>"


def pre(body: str) -> str:
    """Моноширинный блок (таблица, сводка) с экранированием HTML."""
    return f"<pre>{escape_html(body)}</pre>"


def section(screen: str, extra: str = "") -> str:
    """Заголовок раздела: «▸ ЗАЛЫ И ЦЕНЫ · стр. 1/2»."""
    title = SCREEN_TITLES.get(screen, "")
    line = f"▸ <b>{escape_html(title)}</b>"
    if extra:
        line += f" <i>{escape_html(extra)}</i>"
    return line


def table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Моноширинная таблица: столбцы выравниваются по самой длинной ячейке."""
    cells = [[str(cell) for cell in row] for row in rows]
    head = [str(cell) for cell in header]
    widths = [
        max([len(head[index]), *(len(row[index]) for row in cells)])
        if cells
        else len(head[index])
        for index in range(len(head))
    ]

    def line(parts: Sequence[str]) -> str:
        # Первый столбец (название) — по левому краю, числа — по правому.
        cells_out = [parts[0].ljust(widths[0])]
        cells_out += [part.rjust(widths[i]) for i, part in enumerate(parts) if i]
        return "  ".join(cells_out).rstrip()

    return "\n".join([line(head), *(line(row) for row in cells)])


def status_tag(status: str) -> str:
    """Статус заявки в квадратных скобках: «[ ⏳ ОЖИДАЕТ ]»."""
    known = {
        BookingStatus.PENDING: "[ ⏳ ОЖИДАЕТ ]",
        BookingStatus.APPROVED: "[ ✅ ОДОБРЕНО ]",
        BookingStatus.REJECTED: "[ ❌ ОТКЛОНЕНО ]",
    }
    return known.get(status, f"[ • {status.upper()} ]")


# --------------------------------------------------------------------------- #
# Клавиатуры
# --------------------------------------------------------------------------- #
def keyboard(*rows: Sequence[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Собрать inline-клавиатуру из строк кнопок ``(текст, callback_data)``."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


#: Служебный экран «вернуться назад» — навигатор обрабатывает его сам,
#: снимая предыдущий экран со стека переходов.
SCREEN_BACK: Final[str] = "back"


def back_row(*, home: bool = True) -> list[tuple[str, str]]:
    """Служебная строка навигации: «◀ Назад» (+ «🏠 В меню»)."""
    row: list[tuple[str, str]] = [(BTN_BACK, nav(SCREEN_BACK))]
    if home:
        row.append((BTN_HOME, nav(Screen.MENU)))
    return row


def footer_rows(
    *rows: Sequence[tuple[str, str]],
    home: bool = True,
) -> InlineKeyboardMarkup:
    """Клавиатура с содержимым и служебной строкой «Назад / В меню» внизу."""
    return keyboard(*rows, back_row(home=home))


def menu_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    """Главное меню клуба. Кнопка админ-панели видна только администраторам.

    Проверка прав выполняется на месте — Telegram не показывает кнопку тем,
    у кого нет доступа, потому что мы её просто не присылаем.
    """
    rows: list[Sequence[tuple[str, str]]] = [
        [
            ("🖥 Залы и цены", nav(Screen.PRICES)),
            ("🎮 Забронировать", nav(Screen.BOOK_TARIFF)),
        ],
        [("📋 Мои брони", nav(Screen.BOOKINGS)), ("❓ FAQ", nav(Screen.FAQ))],
        [("📜 Правила", nav(Screen.RULES)), ("📞 Контакты", nav(Screen.CONTACTS))],
    ]
    if is_admin:
        rows.append([("🛠 Админ-панель", nav(Screen.ADMIN))])
    return keyboard(*rows)


def faq_list_keyboard() -> InlineKeyboardMarkup:
    """Список вопросов: по кнопке на вопрос, ответ открывается в этом же сообщении."""
    return footer_rows(
        *[
            ((f"{item.emoji} {item.question}", nav(Screen.FAQ_ITEM, item.key)),)
            for item in texts.FAQ_ITEMS
        ]
    )


def faq_answer_keyboard() -> InlineKeyboardMarkup:
    """Экран ответа: возврат к списку вопросов и в меню."""
    return keyboard([("◀ К вопросам", nav(Screen.FAQ))], back_row())


__all__ = [
    "BOOKING_SCREENS",
    "BTN_BACK",
    "BTN_CANCEL",
    "BTN_HOME",
    "BTN_REFRESH",
    "BTN_STEP_BACK",
    "HUD_WIDTH",
    "SCREEN_BACK",
    "SCREEN_TITLES",
    "Screen",
    "ScreenCB",
    "back_row",
    "faq_answer_keyboard",
    "faq_list_keyboard",
    "footer_rows",
    "hud",
    "keyboard",
    "menu_keyboard",
    "nav",
    "pre",
    "section",
    "status_tag",
    "table",
]
