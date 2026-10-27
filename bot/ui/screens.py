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
from bot.ui.palette import (
    FAQ_TONES,
    RULES_TAB_TONES,
    Surface,
    Tone,
    apply_tone_icon,
)
from bot.utils.formatting import escape_html

_SUPPORTS_STYLE: Final[bool] = "style" in InlineKeyboardButton.model_fields
_SUPPORTS_COPY: Final[bool] = "copy_text" in InlineKeyboardButton.model_fields
_SUPPORTS_CUSTOM_EMOJI: Final[bool] = (
    "icon_custom_emoji_id" in InlineKeyboardButton.model_fields
)


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

    #: Бронирование, шаг 0 — выбор точки сети (если точек >= 2).
    BOOK_BRANCH: Final[str] = "book_branch"
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

    #: Админ-панель: главный экран и очередь броней.
    ADMIN: Final[str] = "admin"
    #: Очередь новых броней (в ``arg`` — номер страницы).
    ADMIN_PENDING: Final[str] = "admin_pending"
    #: Подтверждённые сегодня брони (в ``arg`` — номер страницы).
    ADMIN_APPROVED: Final[str] = "admin_approved"
    #: Отклонённые / отменённые сегодня брони (в ``arg`` — номер страницы).
    ADMIN_REJECTED: Final[str] = "admin_rejected"
    #: Детальная карточка конкретной брони (в ``arg`` — ID брони).
    ADMIN_BOOKING: Final[str] = "admin_booking"
    #: Выбор причины отклонения брони (в ``arg`` — ID брони).
    ADMIN_BOOKING_REJECT: Final[str] = "admin_bk_rej"
    #: Выбор причины отмены подтверждённой брони (в ``arg`` — ID брони).
    ADMIN_BOOKING_CANCEL: Final[str] = "admin_bk_can"
    #: Изменение параметров брони: время, гости, ПК (в ``arg`` — ID брони).
    ADMIN_BOOKING_EDIT: Final[str] = "admin_bk_edit"
    #: Мастер ручного создания брони за гостя.
    ADMIN_MANUAL: Final[str] = "admin_manual"
    #: Расписание работы, слоты, буфер и занятость.
    ADMIN_SCHEDULE: Final[str] = "admin_schedule"
    #: Тарифы (будни/выходные) и доп. услуги.
    ADMIN_TARIFFS: Final[str] = "admin_tariffs"
    #: Карточка настройки тарифа зала (в ``arg`` — ключ зала).
    ADMIN_TARIFF_ITEM: Final[str] = "admin_tariff"
    #: Точки сети клубов.
    ADMIN_BRANCHES: Final[str] = "admin_branches"
    #: Карточка конкретной точки сети (в ``arg`` — ID точки).
    ADMIN_BRANCH_ITEM: Final[str] = "admin_branch"
    #: Список администраторов.
    ADMIN_ADMINS: Final[str] = "admin_admins"
    #: Карточка администратора (в ``arg`` — Telegram ID).
    ADMIN_ADMIN: Final[str] = "admin_admin"
    #: Добавление администратора (Имя + ID или инвайт-ссылка).
    ADMIN_ADD: Final[str] = "admin_add"
    #: Передача точки другому администратору (в ``arg`` — user_id сдающего).
    ADMIN_HANDOVER: Final[str] = "admin_handover"
    #: Настройки пуш-уведомлений и «тихих часов» текущего админа.
    ADMIN_NOTIFY: Final[str] = "admin_notify"
    #: Настройки клуба: название, чат, реквизиты, приветствие, правила брони.
    ADMIN_SETTINGS: Final[str] = "admin_settings"

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
    Screen.BOOK_BRANCH: "БРОНЬ · ВЫБОР ТОЧКИ",
    Screen.BOOK_TARIFF: "БРОНЬ · ШАГ 1/4 · ЗАЛ",
    Screen.BOOK_TIME: "БРОНЬ · ШАГ 2/4 · ВРЕМЯ",
    Screen.BOOK_TIME_TEXT: "БРОНЬ · ШАГ 2/4 · ВРЕМЯ",
    Screen.BOOK_NAME: "БРОНЬ · ШАГ 3/4 · ИМЯ",
    Screen.BOOK_PAY: "БРОНЬ · ШАГ 4/4 · ОПЛАТА",
    Screen.BOOK_RECEIPT: "БРОНЬ · ШАГ 4/4 · ЧЕК",
    Screen.BOOK_DONE: "ЗАЯВКА ОТПРАВЛЕНА",
    Screen.ADMIN: "АДМИН-ПАНЕЛЬ · ОЧЕРЕДЬ",
    Screen.ADMIN_PENDING: "АКТИВНЫЕ БРОНИ · НОВЫЕ",
    Screen.ADMIN_APPROVED: "ПОДТВЕРЖДЁННЫЕ СЕГОДНЯ",
    Screen.ADMIN_REJECTED: "ОТКЛОНЁННЫЕ СЕГОДНЯ",
    Screen.ADMIN_BOOKING: "КАРТОЧКА БРОНИ",
    Screen.ADMIN_BOOKING_REJECT: "ОТКЛОНЕНИЕ БРОНИ",
    Screen.ADMIN_BOOKING_CANCEL: "ОТМЕНА БРОНИ",
    Screen.ADMIN_BOOKING_EDIT: "ИЗМЕНЕНИЕ БРОНИ",
    Screen.ADMIN_MANUAL: "РУЧНОЕ СОЗДАНИЕ БРОНИ",
    Screen.ADMIN_SCHEDULE: "РАСПИСАНИЕ И СЛОТЫ",
    Screen.ADMIN_TARIFFS: "ТАРИФЫ И ДОП. УСЛУГИ",
    Screen.ADMIN_TARIFF_ITEM: "НАСТРОЙКА ТАРИФА",
    Screen.ADMIN_BRANCHES: "ТОЧКИ СЕТИ",
    Screen.ADMIN_BRANCH_ITEM: "КАРТОЧКА ТОЧКИ",
    Screen.ADMIN_ADMINS: "АДМИНИСТРАТОРЫ",
    Screen.ADMIN_ADMIN: "КАРТОЧКА АДМИНИСТРАТОРА",
    Screen.ADMIN_ADD: "НОВЫЙ АДМИНИСТРАТОР",
    Screen.ADMIN_HANDOVER: "ПЕРЕДАЧА ТОЧКИ",
    Screen.ADMIN_NOTIFY: "УВЕДОМЛЕНИЯ АДМИНУ",
    Screen.ADMIN_SETTINGS: "НАСТРОЙКИ КЛУБА",
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
    Screen.BOOK_BRANCH: "🏢",
    Screen.BOOK_TARIFF: "🎮",
    Screen.BOOK_TIME: "🕒",
    Screen.BOOK_TIME_TEXT: "✏️",
    Screen.BOOK_NAME: "👤",
    Screen.BOOK_PAY: "💳",
    Screen.BOOK_RECEIPT: "📸",
    Screen.BOOK_DONE: "✅",
    Screen.ADMIN: "🛠",
    Screen.ADMIN_PENDING: "📋",
    Screen.ADMIN_APPROVED: "✅",
    Screen.ADMIN_REJECTED: "🚫",
    Screen.ADMIN_BOOKING: "🎫",
    Screen.ADMIN_BOOKING_REJECT: "❌",
    Screen.ADMIN_BOOKING_CANCEL: "🚫",
    Screen.ADMIN_BOOKING_EDIT: "✏️",
    Screen.ADMIN_MANUAL: "➕",
    Screen.ADMIN_SCHEDULE: "🗓",
    Screen.ADMIN_TARIFFS: "💰",
    Screen.ADMIN_TARIFF_ITEM: "🏷",
    Screen.ADMIN_BRANCHES: "🏢",
    Screen.ADMIN_BRANCH_ITEM: "📍",
    Screen.ADMIN_ADMINS: "👥",
    Screen.ADMIN_ADMIN: "🧑‍💼",
    Screen.ADMIN_ADD: "➕",
    Screen.ADMIN_HANDOVER: "🔄",
    Screen.ADMIN_NOTIFY: "🔔",
    Screen.ADMIN_SETTINGS: "⚙️",
    Screen.PRIVACY: "🛡",
    Screen.PRIVACY_CONFIRM: "🚫",
    Screen.CONSENT: "🛡",
    Screen.CONSENT_REVOKED: "🚫",
}

#: Экраны админ-панели (закрыты фильтром прав в cb_navigate).
ADMIN_ONLY_SCREENS: Final[frozenset[str]] = frozenset(
    {
        Screen.ADMIN,
        Screen.ADMIN_PENDING,
        Screen.ADMIN_APPROVED,
        Screen.ADMIN_REJECTED,
        Screen.ADMIN_BOOKING,
        Screen.ADMIN_BOOKING_REJECT,
        Screen.ADMIN_BOOKING_CANCEL,
        Screen.ADMIN_BOOKING_EDIT,
        Screen.ADMIN_MANUAL,
        Screen.ADMIN_SCHEDULE,
        Screen.ADMIN_TARIFFS,
        Screen.ADMIN_TARIFF_ITEM,
        Screen.ADMIN_BRANCHES,
        Screen.ADMIN_BRANCH_ITEM,
        Screen.ADMIN_ADMINS,
        Screen.ADMIN_ADMIN,
        Screen.ADMIN_ADD,
        Screen.ADMIN_HANDOVER,
        Screen.ADMIN_NOTIFY,
        Screen.ADMIN_SETTINGS,
    }
)

#: Экраны-шаги бронирования: внутри мастера «Шаг назад» ведёт к предыдущему шагу,
#: а выход кнопкой «Назад» поднимает в глобальное главное меню.
BOOKING_SCREENS: Final[frozenset[str]] = frozenset(
    {
        Screen.BOOK_BRANCH,
        Screen.BOOK_TARIFF,
        Screen.BOOK_TIME,
        Screen.BOOK_TIME_TEXT,
        Screen.BOOK_NAME,
        Screen.BOOK_PAY,
        Screen.BOOK_RECEIPT,
    }
)

#: Строгая глобальная иерархия экранов («родительский раздел» для кнопки «⬅️ Назад»).
#: Кнопка «Назад» ВСЕГДА поднимает пользователя на один уровень вверх в глобальном
#: дереве интерфейса, а не отматывает хронологическую историю прошлых действий.
SCREEN_PARENTS: Final[dict[str, str]] = {
    # Корень интерфейса
    Screen.MENU: Screen.MENU,
    # Клиентские разделы 1-го уровня → Главное меню
    Screen.PRICES: Screen.MENU,
    Screen.RULES: Screen.MENU,
    Screen.FAQ: Screen.MENU,
    Screen.CONTACTS: Screen.MENU,
    Screen.BOOKINGS: Screen.MENU,
    Screen.PRIVACY: Screen.MENU,
    Screen.BOOK_DONE: Screen.MENU,
    # Клиентские экраны 2-го уровня → их родительский раздел
    Screen.FAQ_ITEM: Screen.FAQ,
    Screen.PRIVACY_CONFIRM: Screen.PRIVACY,
    # Раздел бронирования в глобальной иерархии подчиняется Главному меню
    Screen.BOOK_BRANCH: Screen.MENU,
    Screen.BOOK_TARIFF: Screen.MENU,
    Screen.BOOK_TIME: Screen.MENU,
    Screen.BOOK_TIME_TEXT: Screen.MENU,
    Screen.BOOK_NAME: Screen.MENU,
    Screen.BOOK_PAY: Screen.MENU,
    Screen.BOOK_RECEIPT: Screen.MENU,
    # Экраны согласия 152-ФЗ
    Screen.CONSENT: Screen.CONSENT,
    Screen.CONSENT_REVOKED: Screen.CONSENT_REVOKED,
    # Корень админ-панели → Главное меню
    Screen.ADMIN: Screen.MENU,
    # Разделы админ-панели 1-го уровня → Главный экран админ-панели
    Screen.ADMIN_PENDING: Screen.ADMIN,
    Screen.ADMIN_APPROVED: Screen.ADMIN,
    Screen.ADMIN_REJECTED: Screen.ADMIN,
    Screen.ADMIN_MANUAL: Screen.ADMIN,
    Screen.ADMIN_SCHEDULE: Screen.ADMIN,
    Screen.ADMIN_TARIFFS: Screen.ADMIN,
    Screen.ADMIN_BRANCHES: Screen.ADMIN,
    Screen.ADMIN_ADMINS: Screen.ADMIN,
    Screen.ADMIN_NOTIFY: Screen.ADMIN,
    Screen.ADMIN_SETTINGS: Screen.ADMIN,
    # Карточки сущностей 2-го уровня админ-панели → их списки
    Screen.ADMIN_BOOKING: Screen.ADMIN_PENDING,
    Screen.ADMIN_TARIFF_ITEM: Screen.ADMIN_TARIFFS,
    Screen.ADMIN_BRANCH_ITEM: Screen.ADMIN_BRANCHES,
    Screen.ADMIN_ADMIN: Screen.ADMIN_ADMINS,
    Screen.ADMIN_ADD: Screen.ADMIN_ADMINS,
    # Действия над конкретной сущностью 3-го уровня → карточка этой сущности
    Screen.ADMIN_BOOKING_REJECT: Screen.ADMIN_BOOKING,
    Screen.ADMIN_BOOKING_CANCEL: Screen.ADMIN_BOOKING,
    Screen.ADMIN_BOOKING_EDIT: Screen.ADMIN_BOOKING,
    Screen.ADMIN_HANDOVER: Screen.ADMIN_ADMIN,
}

#: Подэкраны, чей ``arg`` (ID брони или ID администратора) передаётся родительской карточке.
PARENT_PRESERVES_ARG: Final[frozenset[str]] = frozenset(
    {
        Screen.ADMIN_BOOKING_REJECT,
        Screen.ADMIN_BOOKING_CANCEL,
        Screen.ADMIN_BOOKING_EDIT,
        Screen.ADMIN_HANDOVER,
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
BTN_BACK: Final[str] = "⬅️ Назад"
BTN_HOME: Final[str] = "🏠 В меню"
BTN_REFRESH: Final[str] = "🔄 Обновить"
BTN_STEP_BACK: Final[str] = "↩️ Шаг назад"
BTN_CANCEL: Final[str] = "❌ Отменить"


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
ButtonSpec: TypeAlias = (
    InlineKeyboardButton
    | tuple[str, str]
    | tuple[str, str, str | None]
    | tuple[str, str, str | None, str | None]
)


def btn(
    text: str,
    callback_data: str | None = None,
    *,
    style: str | None = None,
    tone: str | None = None,
    copy_value: str | None = None,
    url: str | None = None,
) -> InlineKeyboardButton:
    """Собрать inline-кнопку с поддержкой 10-тоновой палитры, ``style`` и ``CopyTextButton``."""
    rendered_text, custom_emoji_id = apply_tone_icon(text, tone)
    kwargs: dict[str, object] = {"text": rendered_text}
    if style and _SUPPORTS_STYLE:
        kwargs["style"] = style
    if custom_emoji_id and _SUPPORTS_CUSTOM_EMOJI:
        kwargs["icon_custom_emoji_id"] = custom_emoji_id
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
    if len(spec) == 4:
        text, data, style, tone = spec
        return btn(text, data, style=style, tone=tone)
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
    """Спокойная служебная строка навигации на матовом стекле: «◀ Назад» и «🏠 В меню»."""
    row: list[ButtonSpec] = [(BTN_BACK, nav(SCREEN_BACK), Surface.GLASS, Tone.SLATE)]
    if home:
        row.append((BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE))
    return row


def footer_rows(
    *rows: Sequence[ButtonSpec],
    home: bool = True,
) -> InlineKeyboardMarkup:
    """Клавиатура с содержимым и служебной строкой «Назад / В меню» внизу."""
    return keyboard(*rows, back_row(home=home))


def menu_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    """Главное меню клуба по правилу 60–30–10 (богатая палитра без «гирлянды»).

    * 10 % (Hero ``success``): единственная главная CTA-кнопка «🎮 Забронировать место».
    * 20 % (Accent ``primary``): симметричная ключевая пара «🖥 Залы и цены» | «📋 Мои брони».
    * 70 % (Glass ``None``): спокойное матовое стекло с семантическими тонами
      Amber / Indigo / Jade / Amethyst / Tangerine без ложных красных тревог.
    """
    rows: list[Sequence[ButtonSpec]] = [
        [(BTN_BOOK_HERO, nav(Screen.BOOK_TARIFF), Surface.HERO, Tone.MINT)],
        [
            ("🖥 Залы и цены", nav(Screen.PRICES), Surface.ACCENT, Tone.CRYO),
            ("📋 Мои брони", nav(Screen.BOOKINGS), Surface.ACCENT, Tone.SAPPHIRE),
        ],
        [
            ("💡 Частые вопросы", nav(Screen.FAQ), Surface.GLASS, Tone.AMBER),
            ("📘 Правила", nav(Screen.RULES), Surface.GLASS, Tone.INDIGO),
        ],
        [
            ("📍 Контакты", nav(Screen.CONTACTS), Surface.GLASS, Tone.JADE),
            ("🛡 152-ФЗ", nav(Screen.PRIVACY), Surface.GLASS, Tone.AMETHYST),
        ],
    ]
    if is_admin:
        rows.append(
            [("🛠 Админ-панель", nav(Screen.ADMIN), Surface.GLASS, Tone.TANGERINE)]
        )
    return keyboard(*rows)


def rules_tabs_keyboard(active_tab: str) -> InlineKeyboardMarkup:
    """Переключатель вкладок «Правила клуба» (подсвечена только активная вкладка)."""
    tabs = [
        ("📌 Основное", texts.RULES_TAB_MAIN),
        ("💳 Оплата", texts.RULES_TAB_PAY),
        ("🎮 В клубе", texts.RULES_TAB_CLUB),
    ]
    tab_row: list[ButtonSpec] = [
        (
            label,
            nav(Screen.RULES, key),
            Surface.ACCENT if key == active_tab else Surface.GLASS,
            RULES_TAB_TONES.get(key, Tone.INDIGO),
        )
        for label, key in tabs
    ]
    return footer_rows(
        tab_row,
        [(BTN_BOOK_HERO, nav(Screen.BOOK_TARIFF), Surface.HERO, Tone.MINT)],
    )


def faq_list_keyboard() -> InlineKeyboardMarkup:
    """Список частых вопросов на матовом стекле с 8 категорийными тонами палитры."""
    return footer_rows(
        *[
            (
                (
                    f"{item.emoji} {item.question}",
                    nav(Screen.FAQ_ITEM, item.key),
                    Surface.GLASS,
                    FAQ_TONES.get(item.key, Tone.SAPPHIRE),
                ),
            )
            for item in texts.FAQ_ITEMS
        ]
    )


def faq_answer_keyboard() -> InlineKeyboardMarkup:
    """Экран ответа FAQ: единый акцент возврата к списку и спокойный подвал."""
    return keyboard(
        [("📚 К списку вопросов", nav(Screen.FAQ), Surface.ACCENT, Tone.SAPPHIRE)],
        back_row(),
    )


__all__ = [
    "ADMIN_ONLY_SCREENS",
    "BOOKING_SCREENS",
    "BTN_BACK",
    "BTN_BOOK_HERO",
    "BTN_CANCEL",
    "BTN_HOME",
    "BTN_REFRESH",
    "BTN_STEP_BACK",
    "HUD_WIDTH",
    "PARENT_PRESERVES_ARG",
    "SCREEN_BACK",
    "SCREEN_ICONS",
    "SCREEN_PARENTS",
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
