"""Экраны графического интерфейса: идентификаторы, кнопки и подписи.

Интерфейс бота построен на графических фото-карточках демо-клуба NOVA ARENA:

* у пользователя всегда **ровно один** актуальный экран в чате — бот отправляет
  фото-карточку один раз (``sendPhoto``) и при всех переходах обновляет её на
  месте (``editMessageMedia``);
* никаких блоков ``<pre>`` с плашкой «копировать» и псевдографики из дефисов —
  вся визуальная подача (тарифная сетка, шаги брони, билет, дашборд админа)
  отрисована на HD-карточках (:mod:`bot.ui.cards`), а подпись использует чистую
  HTML-вёрстку Telegram (``<blockquote>``, ``<b>``, ``<code>``);
* кнопки на всех экранах раскрашены нативными цветами Bot API (``success``,
  ``primary``, ``danger``), а реквизиты и телефон снабжены кнопками копирования
  в один тап (:class:`aiogram.types.CopyTextButton`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, TypeAlias

from aiogram.filters.callback_data import CallbackData
from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from bot import texts
from bot.db.models import BookingStatus
from bot.utils.formatting import escape_html

_SUPPORTS_STYLE: Final[bool] = "style" in InlineKeyboardButton.model_fields
_SUPPORTS_COPY: Final[bool] = "copy_text" in InlineKeyboardButton.model_fields


# --------------------------------------------------------------------------- #
# Идентификаторы экранов
# --------------------------------------------------------------------------- #
class Screen:
    """Экраны бота. Значение — часть ``callback_data``, поэтому короткое."""

    #: Главное меню.
    MENU: Final[str] = "menu"
    #: Залы и цены.
    PRICES: Final[str] = "prices"
    #: Правила клуба (в ``arg`` — вкладка: ``main`` / ``pay`` / ``club``).
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
    #: Подтверждение отзыва согласия.
    PRIVACY_CONFIRM: Final[str] = "privacy_confirm"
    #: Стартовый запрос согласия (152-ФЗ) до допуска к меню.
    CONSENT: Final[str] = "consent"
    #: Экран после отзыва согласия (бот остановлен до нового согласия).
    CONSENT_REVOKED: Final[str] = "consent_revoked"


#: Заголовки экранов — аккуратная верхняя строка подписи под карточкой.
SCREEN_TITLES: Final[dict[str, str]] = {
    Screen.MENU: "ГЛАВНОЕ МЕНЮ",
    Screen.PRICES: "ЗАЛЫ И ЦЕНЫ",
    Screen.RULES: "ПРАВИЛА КЛУБА",
    Screen.FAQ: "ЧАСТЫЕ ВОПРОСЫ",
    Screen.FAQ_ITEM: "ЧАСТЫЕ ВОПРОСЫ",
    Screen.CONTACTS: "КОНТАКТЫ И ОПЛАТА",
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
    Screen.ADMIN_ADMIN: "КАРТОЧКА АДМИНИСТРАТОРА",
    Screen.ADMIN_ADD: "НОВЫЙ АДМИНИСТРАТОР",
    Screen.PRIVACY: "ПЕРСОНАЛЬНЫЕ ДАННЫЕ (152-ФЗ)",
    Screen.PRIVACY_CONFIRM: "ОТЗЫВ СОГЛАСИЯ (152-ФЗ)",
    Screen.CONSENT: "СОГЛАСИЕ НА ОБРАБОТКУ ДАННЫХ (152-ФЗ)",
    Screen.CONSENT_REVOKED: "ДОСТУП ПРИОСТАНОВЛЕН (152-ФЗ)",
}

SCREEN_ICONS: Final[dict[str, str]] = {
    Screen.MENU: "🔥",
    Screen.PRICES: "🖥",
    Screen.RULES: "📜",
    Screen.FAQ: "❓",
    Screen.FAQ_ITEM: "💬",
    Screen.CONTACTS: "📞",
    Screen.BOOKINGS: "📋",
    Screen.BOOK_TARIFF: "🎮",
    Screen.BOOK_TIME: "🕒",
    Screen.BOOK_TIME_TEXT: "✏️",
    Screen.BOOK_NAME: "👤",
    Screen.BOOK_PAY: "💳",
    Screen.BOOK_RECEIPT: "📸",
    Screen.BOOK_DONE: "✅",
    Screen.ADMIN: "🛠",
    Screen.ADMIN_PENDING: "📋",
    Screen.ADMIN_ADMINS: "👥",
    Screen.ADMIN_ADMIN: "🧑‍💼",
    Screen.ADMIN_ADD: "➕",
    Screen.PRIVACY: "🛡",
    Screen.PRIVACY_CONFIRM: "🚫",
    Screen.CONSENT: "🛡",
    Screen.CONSENT_REVOKED: "🚫",
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
# Подписи кнопок и текстовые элементы (без <pre> в рабочих экранах)
# --------------------------------------------------------------------------- #
HUD_WIDTH: Final[int] = 28

BTN_BOOK_HERO: Final[str] = "🎮 Забронировать место"
BTN_BACK: Final[str] = "◀ Назад"
BTN_HOME: Final[str] = "🏠 В меню"
BTN_REFRESH: Final[str] = "🔄 Обновить"
BTN_STEP_BACK: Final[str] = "‹ Шаг назад"
BTN_CANCEL: Final[str] = "✕ Отменить"


def section(screen: str, club_name: str = "", extra: str = "") -> str:
    """Чистая строка заголовка раздела без блоков кода ``<pre>``."""
    icon = SCREEN_ICONS.get(screen, "⚡️")
    title = SCREEN_TITLES.get(screen, "")
    prefix = f"<b>{escape_html(club_name)}</b> · " if club_name else ""
    line = f"{icon} {prefix}<b>{escape_html(title)}</b>"
    if extra:
        line += f"  <i>({escape_html(extra)})</i>"
    return line


def hud(title: str, subtitle: str = "") -> str:
    """Компактная плашка заголовка (сохранена для совместимости без ``<pre>``)."""
    sub = f" · {escape_html(subtitle)}" if subtitle else ""
    return f"<b>{escape_html(title)}</b>{sub}"


def pre(body: str) -> str:
    """Цитатный блок вместо ``<pre>`` (без серой шапки «копировать»)."""
    return f"<blockquote>{escape_html(body)}</blockquote>"


def table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Текстовая таблица с выравниванием столбцов."""
    cells = [[str(cell) for cell in row] for row in rows]
    head = [str(cell) for cell in header]
    widths = [
        max([len(head[index]), *(len(row[index]) for row in cells)])
        if cells
        else len(head[index])
        for index in range(len(head))
    ]

    def line(parts: Sequence[str]) -> str:
        cells_out = [parts[0].ljust(widths[0])]
        cells_out += [part.rjust(widths[i]) for i, part in enumerate(parts) if i]
        return "  ".join(cells_out).rstrip()

    return "\n".join([line(head), *(line(row) for row in cells)])


def status_tag(status: str) -> str:
    """Статус заявки: «⏳ Ожидает подтверждения» / «✅ Одобрена» / «❌ Отклонена»."""
    known = {
        BookingStatus.PENDING: "⏳ <b>Ожидает</b>",
        BookingStatus.APPROVED: "✅ <b>Одобрена</b>",
        BookingStatus.REJECTED: "❌ <b>Отклонена</b>",
    }
    return known.get(status, f"• {escape_html(status)}")


# --------------------------------------------------------------------------- #
# Конструкторы кнопок и клавиатур
# --------------------------------------------------------------------------- #
ButtonSpec: TypeAlias = InlineKeyboardButton | tuple[str, str] | tuple[str, str, str]


def btn(
    text: str,
    callback_data: str | None = None,
    *,
    style: str | None = None,
    copy_value: str | None = None,
    url: str | None = None,
) -> InlineKeyboardButton:
    """Собрать inline-кнопку с поддержкой цвета Bot API и ``CopyTextButton``."""
    kwargs: dict[str, object] = {"text": text}
    if style and _SUPPORTS_STYLE:
        kwargs["style"] = style
    if copy_value is not None and _SUPPORTS_COPY:
        kwargs["copy_text"] = CopyTextButton(text=copy_value)
    elif url is not None:
        kwargs["url"] = url
    else:
        kwargs["callback_data"] = callback_data or nav(Screen.MENU)
    return InlineKeyboardButton(**kwargs)  # type: ignore[arg-type]


def _coerce_button(spec: ButtonSpec) -> InlineKeyboardButton:
    if isinstance(spec, InlineKeyboardButton):
        return spec
    if len(spec) == 3:
        text, data, style = spec
        return btn(text, data, style=style)
    text, data = spec
    return btn(text, data)


def keyboard(*rows: Sequence[ButtonSpec]) -> InlineKeyboardMarkup:
    """Собрать inline-клавиатуру из строк кнопок."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[_coerce_button(item) for item in row] for row in rows if row]
    )


#: Служебный экран «вернуться назад» — навигатор снимает экран со стека.
SCREEN_BACK: Final[str] = "back"


def back_row(*, home: bool = True) -> list[ButtonSpec]:
    """Служебная строка навигации: «◀ Назад» (+ цветная «🏠 В меню»)."""
    row: list[ButtonSpec] = [(BTN_BACK, nav(SCREEN_BACK))]
    if home:
        row.append((BTN_HOME, nav(Screen.MENU), "primary"))
    return row


def footer_rows(
    *rows: Sequence[ButtonSpec],
    home: bool = True,
) -> InlineKeyboardMarkup:
    """Клавиатура с содержимым и служебной строкой «Назад / В меню» внизу."""
    return keyboard(*rows, back_row(home=home))


def menu_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    """Главное меню клуба с многоцветными акцентами Bot API.

    * Первая строка — яркая зелёная CTA-кнопка «🎮 Забронировать место» (``success``).
    * Ниже — симметричная цветная сетка разделов 2×3 (``primary``, ``success``, ``danger``).
    * Кнопка «🛠 Админ-панель» добавляется только активным администраторам.
    """
    rows: list[Sequence[ButtonSpec]] = [
        [(BTN_BOOK_HERO, nav(Screen.BOOK_TARIFF), "success")],
        [
            ("🖥 Залы и цены", nav(Screen.PRICES), "primary"),
            ("📋 Мои брони", nav(Screen.BOOKINGS), "primary"),
        ],
        [
            ("❓ FAQ", nav(Screen.FAQ), "primary"),
            ("📜 Правила", nav(Screen.RULES)),
        ],
        [
            ("📞 Контакты", nav(Screen.CONTACTS), "success"),
            ("🛡 152-ФЗ", nav(Screen.PRIVACY), "danger"),
        ],
    ]
    if is_admin:
        rows.append([("🛠 Админ-панель", nav(Screen.ADMIN), "primary")])
    return keyboard(*rows)


def rules_tabs_keyboard(active_tab: str) -> InlineKeyboardMarkup:
    """Цветные вкладки раздела «Правила клуба» + переход к бронированию."""
    tabs = [
        ("📌 Основное", texts.RULES_TAB_MAIN),
        ("💳 Оплата", texts.RULES_TAB_PAY),
        ("🎮 В клубе", texts.RULES_TAB_CLUB),
    ]
    tab_row: list[ButtonSpec] = [
        (
            f"• {label}" if key == active_tab else label,
            nav(Screen.RULES, key),
            "success" if key == active_tab else "primary",
        )
        for label, key in tabs
    ]
    return footer_rows(
        tab_row,
        [(BTN_BOOK_HERO, nav(Screen.BOOK_TARIFF), "success")],
    )


_FAQ_ITEM_STYLES: Final[dict[str, str]] = {
    "night": "primary",
    "packages": "",
    "prepay": "success",
    "payment": "success",
    "pcs": "primary",
    "faceit": "",
    "late": "primary",
    "scam": "danger",
}


def faq_list_keyboard() -> InlineKeyboardMarkup:
    """Список вопросов с цветной подсветкой категорий: ответ открывается на месте."""
    return footer_rows(
        *[
            (
                (
                    f"{item.emoji} {item.question}",
                    nav(Screen.FAQ_ITEM, item.key),
                    _FAQ_ITEM_STYLES.get(item.key, ""),
                ),
            )
            for item in texts.FAQ_ITEMS
        ]
    )


def faq_answer_keyboard() -> InlineKeyboardMarkup:
    """Экран ответа: цветной возврат к списку вопросов и в меню."""
    return keyboard([("◀ К списку вопросов", nav(Screen.FAQ), "success")], back_row())


__all__ = [
    "BOOKING_SCREENS",
    "BTN_BACK",
    "BTN_BOOK_HERO",
    "BTN_CANCEL",
    "BTN_HOME",
    "BTN_REFRESH",
    "BTN_STEP_BACK",
    "HUD_WIDTH",
    "SCREEN_BACK",
    "SCREEN_ICONS",
    "SCREEN_TITLES",
    "ButtonSpec",
    "Screen",
    "ScreenCB",
    "back_row",
    "btn",
    "faq_answer_keyboard",
    "faq_list_keyboard",
    "footer_rows",
    "hud",
    "keyboard",
    "menu_keyboard",
    "nav",
    "pre",
    "rules_tabs_keyboard",
    "section",
    "status_tag",
    "table",
]
