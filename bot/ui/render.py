"""Содержимое экранов: текст с HUD-шапкой и inline-клавиатура.

Каждый экран — отдельная функция ``_render_<screen>(ctx, arg)``, которая читает
базу и FSM-данные и возвращает ``(текст, клавиатура)``. Здесь нет ни одного
вызова Telegram API: что показывать и как править сообщение, решает
:mod:`bot.ui.navigator`.

Контекст :class:`ViewContext` собирается один раз на апдейт и передаётся в
рендереры, поэтому у всех них одинаковые, предсказуемые зависимости.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import User as TelegramUser

from bot import texts
from bot.config import AppConfig
from bot.db.database import Database
from bot.db.models import AdminRole, BookingStatus
from bot.keyboards.inline import (
    CB_ADMIN_ADD,
    CB_BOOKING_BACK,
    CB_BOOKING_CANCEL,
    CB_PAY_CHECK,
    CB_PAY_SPOT,
    CB_TARIFF_PREFIX,
    CB_TIME_PREFIX,
    PENDING_PAGE_SIZE,
    admin_approve_callback,
    admin_delete_callback,
    admin_demote_callback,
    admin_promote_callback,
    admin_reject_callback,
    admin_toggle_callback,
)
from bot.services.admins import AdminRegistry
from bot.states import DATA_CLIENT_NAME, DATA_TARIFF_KEY, DATA_TIME_KEY, DATA_TIME_SLOT
from bot.ui.screens import (
    BTN_BACK,
    BTN_CANCEL,
    BTN_REFRESH,
    BTN_STEP_BACK,
    SCREEN_BACK,
    Screen,
    faq_answer_keyboard,
    faq_list_keyboard,
    footer_rows,
    hud,
    keyboard,
    menu_keyboard,
    nav,
    pre,
    section,
    status_tag,
    table,
)
from bot.utils.formatting import escape_html, format_datetime, now_text, today_date_text, truncate

logger = logging.getLogger(__name__)

#: Ограничение Telegram на длину текста сообщения.
MESSAGE_LIMIT: int = 4096

#: Кнопка «шаг назад» в мастере бронирования.
Button = tuple[str, str]
Row = Sequence[Button]


@dataclass(slots=True)
class ViewContext:
    """Всё, что нужно рендереру экрана: база, конфиг, бот и кто смотрит."""

    db: Database
    cfg: AppConfig
    bot: Bot
    user_id: int
    state: FSMContext | None = None
    admins: AdminRegistry | None = None
    user: TelegramUser | None = None


# --------------------------------------------------------------------------- #
# Мелкие helpers
# --------------------------------------------------------------------------- #
def _subtitle(club: Any) -> str:
    """Вторая строка рамки: «ЧИТА · 24/7» (или просто город)."""
    city = str(club.city).upper()
    return f"{city} · 24/7" if "24" in str(club.hours) else city


def _header(screen: str, club: Any, extra: str = "") -> str:
    """Шапка экрана: рамка клуба + заголовок раздела."""
    return "\n".join([hud(f"▚▚ {club.name} ▚▚", _subtitle(club)), "", section(screen, extra)])


def _compose(screen: str, club: Any, body: str, extra: str = "") -> str:
    """Готовый текст экрана: шапка, пустая строка, содержимое."""
    return truncate(f"{_header(screen, club, extra)}\n\n{body}", MESSAGE_LIMIT)


async def _is_admin(ctx: ViewContext) -> bool:
    """Есть ли у смотрящего права администратора."""
    if ctx.admins is not None:
        return await ctx.admins.is_admin(ctx.user_id)
    if await ctx.db.is_active_admin(ctx.user_id):
        return True
    total, _ = await ctx.db.count_admins()
    if total == 0:
        return ctx.cfg.is_env_admin(ctx.user_id)
    return False


async def _is_owner(ctx: ViewContext) -> bool:
    """Может ли смотрящий управлять списком администраторов."""
    if ctx.admins is not None:
        return await ctx.admins.is_owner(ctx.user_id)
    admin = await ctx.db.get_admin(ctx.user_id)
    if admin is not None:
        return admin.is_owner
    total, _ = await ctx.db.count_admins()
    return total == 0 and ctx.user_id == ctx.cfg.admin_id


async def _fsm(ctx: ViewContext) -> dict[str, Any]:
    """Данные мастера бронирования (пустой словарь, если состояния нет)."""
    if ctx.state is None:
        return {}
    return await ctx.state.get_data()


def _with_step_nav(*rows: Row) -> InlineKeyboardMarkup:
    """Клавиатура шага бронирования: содержимое + «Шаг назад» / «Отменить»."""
    prepared: list[list[Button]] = [[(str(t), str(d)) for t, d in row] for row in rows]
    prepared.append([(BTN_STEP_BACK, CB_BOOKING_BACK), (BTN_CANCEL, CB_BOOKING_CANCEL)])
    return keyboard(*prepared)


def _pairs(rows: Row, per_row: int = 2) -> list[list[Button]]:
    """Разложить кнопки по несколько в строке."""
    flat = [(str(t), str(d)) for t, d in rows]
    return [flat[i : i + per_row] for i in range(0, len(flat), per_row)]


# --------------------------------------------------------------------------- #
# Клиентские экраны
# --------------------------------------------------------------------------- #
async def _render_menu(ctx: ViewContext, _arg: str) -> tuple[str, InlineKeyboardMarkup]:
    body = texts.render(texts.SCREEN_MENU_BODY, ctx.cfg.club)
    return _compose(Screen.MENU, ctx.cfg.club, body), menu_keyboard(await _is_admin(ctx))


async def _render_prices(ctx: ViewContext, _arg: str) -> tuple[str, InlineKeyboardMarkup]:
    club = ctx.cfg.club
    hourly = table(
        ("ЗАЛ", "1Ч", "3Ч", "5Ч"),
        [(t.title, t.hour1, t.hour3, t.hour5) for t in texts.TARIFFS],
    )
    packs = table(
        ("ЗАЛ", "УТРО", "ДЕНЬ", "НОЧЬ"),
        [(t.title, t.morning, t.day, t.night) for t in texts.TARIFFS],
    )
    night_weekend = " · ".join(f"{t.night_weekend} ₽" for t in texts.TARIFFS)
    body = "\n".join(
        [
            texts.render(
                texts.SCREEN_PRICES_BODY,
                club,
                morning_slot=texts.MORNING_SLOT,
                day_slot=texts.DAY_SLOT,
                night_slot=texts.NIGHT_SLOT,
            ),
            "",
            pre(hourly),
            "",
            pre(packs),
            "",
            texts.render(texts.SCREEN_PRICES_NOTE, club, night_weekend=night_weekend),
        ]
    )
    return (
        _compose(Screen.PRICES, club, body),
        footer_rows([("🎮 Забронировать", nav(Screen.BOOK_TARIFF)), ("❓ FAQ", nav(Screen.FAQ))]),
    )


async def _render_rules(ctx: ViewContext, _arg: str) -> tuple[str, InlineKeyboardMarkup]:
    return (
        _compose(Screen.RULES, ctx.cfg.club, texts.build_rules_body(ctx.cfg.club)),
        footer_rows([("🎮 Забронировать", nav(Screen.BOOK_TARIFF))]),
    )


async def _render_faq_list(ctx: ViewContext, _arg: str) -> tuple[str, InlineKeyboardMarkup]:
    body = "Выберите вопрос — ответ откроется здесь же, в этом сообщении."
    return _compose(Screen.FAQ, ctx.cfg.club, body), faq_list_keyboard()


async def _render_faq_item(ctx: ViewContext, arg: str) -> tuple[str, InlineKeyboardMarkup]:
    item = texts.FAQ_BY_KEY.get(arg)
    if item is None:  # вопрос пропал из списка — возвращаем к списку
        return await _render_faq_list(ctx, "")
    body = f"<b>{escape_html(item.question)}</b>\n\n{texts.faq_answer(item, ctx.cfg.club)}"
    return _compose(Screen.FAQ_ITEM, ctx.cfg.club, body), faq_answer_keyboard()


async def _render_contacts(ctx: ViewContext, _arg: str) -> tuple[str, InlineKeyboardMarkup]:
    club = ctx.cfg.club
    return (
        _compose(Screen.CONTACTS, club, texts.build_contacts_body(club)),
        footer_rows(
            [
                ("🎮 Забронировать", nav(Screen.BOOK_TARIFF)),
                ("🖥 Залы и цены", nav(Screen.PRICES)),
            ]
        ),
    )


async def _render_bookings(ctx: ViewContext, _arg: str) -> tuple[str, InlineKeyboardMarkup]:
    bookings = await ctx.db.list_user_bookings(ctx.user_id, limit=10)
    if not bookings:
        body = texts.SCREEN_BOOKINGS_EMPTY
    else:
        body = "\n\n".join(
            texts.SCREEN_BOOKINGS_ITEM.format(
                number=booking.id,
                tariff=escape_html(booking.tariff),
                time_slot=escape_html(booking.time_slot),
                created=format_datetime(booking.created_at, ctx.cfg.timezone, "%d.%m %H:%M"),
                status=status_tag(booking.status),
            )
            for booking in bookings
        )
    return (
        _compose(Screen.BOOKINGS, ctx.cfg.club, body),
        footer_rows([("🎮 Забронировать", nav(Screen.BOOK_TARIFF))]),
    )


# --- мастер бронирования ---------------------------------------------------- #
async def _render_book_tariff(ctx: ViewContext, _arg: str) -> tuple[str, InlineKeyboardMarkup]:
    body = texts.render(
        texts.SCREEN_BOOK_TARIFF_BODY, ctx.cfg.club, tariffs_overview=texts.build_tariffs_overview()
    )
    rows = _pairs(
        [
            (f"{tariff.emoji} {tariff.title} · {tariff.hour1} ₽/ч", f"{CB_TARIFF_PREFIX}{tariff.key}")
            for tariff in texts.TARIFFS
        ]
    )
    rows.append([(BTN_BACK, CB_BOOKING_BACK), (BTN_CANCEL, CB_BOOKING_CANCEL)])
    return _compose(Screen.BOOK_TARIFF, ctx.cfg.club, body), keyboard(*rows)


async def _render_book_time(ctx: ViewContext, _arg: str) -> tuple[str, InlineKeyboardMarkup]:
    data = await _fsm(ctx)
    tariff = texts.get_tariff(str(data.get(DATA_TARIFF_KEY) or ""))
    if tariff is None:
        return await _render_book_tariff(ctx, "")

    moment = datetime.now(ctx.cfg.timezone)
    body = texts.render(
        texts.SCREEN_BOOK_TIME_BODY,
        ctx.cfg.club,
        tariff=escape_html(tariff.title),
        now_time=now_text(ctx.cfg.timezone),
        now_date=today_date_text(ctx.cfg.timezone),
    )
    rows = _pairs([(label, f"{CB_TIME_PREFIX}{key}") for key, label in texts.TIME_CHOICES])
    return (
        _compose(Screen.BOOK_TIME, ctx.cfg.club, body, extra=moment.strftime("%H:%M")),
        _with_step_nav(*rows),
    )


async def _render_book_time_text(ctx: ViewContext, _arg: str) -> tuple[str, InlineKeyboardMarkup]:
    data = await _fsm(ctx)
    tariff = texts.get_tariff(str(data.get(DATA_TARIFF_KEY) or ""))
    body = texts.render(
        texts.SCREEN_BOOK_TIME_TEXT_BODY,
        ctx.cfg.club,
        tariff=escape_html(tariff.title) if tariff else "—",
    )
    return _compose(Screen.BOOK_TIME_TEXT, ctx.cfg.club, body), _with_step_nav()


async def _render_book_name(ctx: ViewContext, _arg: str) -> tuple[str, InlineKeyboardMarkup]:
    data = await _fsm(ctx)
    tariff = texts.get_tariff(str(data.get(DATA_TARIFF_KEY) or ""))
    body = texts.render(
        texts.SCREEN_BOOK_NAME_BODY,
        ctx.cfg.club,
        tariff=escape_html(tariff.title) if tariff else "—",
        time_slot=escape_html(str(data.get(DATA_TIME_SLOT) or "—")),
    )
    return _compose(Screen.BOOK_NAME, ctx.cfg.club, body), _with_step_nav()


async def _render_book_pay(ctx: ViewContext, _arg: str) -> tuple[str, InlineKeyboardMarkup]:
    data = await _fsm(ctx)
    tariff = texts.get_tariff(str(data.get(DATA_TARIFF_KEY) or ""))
    time_key = str(data.get(DATA_TIME_KEY) or "")
    today = datetime.now(ctx.cfg.timezone).date()
    required = texts.prepayment_required(time_key, today)

    body = texts.render(
        texts.SCREEN_BOOK_PAY_BODY,
        ctx.cfg.club,
        tariff=escape_html(tariff.title) if tariff else "—",
        time_slot=escape_html(str(data.get(DATA_TIME_SLOT) or "—")),
        client_name=escape_html(str(data.get(DATA_CLIENT_NAME) or "—")),
        amount=texts.amount_text(str(data.get(DATA_TARIFF_KEY) or ""), time_key, today),
        payment_rules=texts.render(
            texts.PAYMENT_RULES_PREPAID if required else texts.PAYMENT_RULES_ON_SPOT,
            ctx.cfg.club,
        ),
    )

    rows: list[list[Button]] = [[("📸 Оплатил — отправить чек", CB_PAY_CHECK)]]
    if not required:
        rows.append([("💵 Оплачу на месте", CB_PAY_SPOT)])
    return _compose(Screen.BOOK_PAY, ctx.cfg.club, body), _with_step_nav(*rows)


async def _render_book_receipt(ctx: ViewContext, _arg: str) -> tuple[str, InlineKeyboardMarkup]:
    data = await _fsm(ctx)
    today = datetime.now(ctx.cfg.timezone).date()
    body = texts.render(
        texts.SCREEN_BOOK_RECEIPT_BODY,
        ctx.cfg.club,
        amount=texts.amount_text(
            str(data.get(DATA_TARIFF_KEY) or ""), str(data.get(DATA_TIME_KEY) or ""), today
        ),
    )
    return _compose(Screen.BOOK_RECEIPT, ctx.cfg.club, body), _with_step_nav()


async def _render_book_done(ctx: ViewContext, arg: str) -> tuple[str, InlineKeyboardMarkup]:
    try:
        booking_id = int(arg)
    except (TypeError, ValueError):
        return await _render_menu(ctx, "")

    booking = await ctx.db.get_booking(booking_id)
    if booking is None:
        return await _render_menu(ctx, "")

    body = texts.render(
        texts.SCREEN_BOOK_DONE_BODY,
        ctx.cfg.club,
        booking_id=booking.id,
        tariff=escape_html(booking.tariff),
        time_slot=escape_html(booking.time_slot),
        client_name=escape_html(booking.client_name),
        payment_note=texts.SCREEN_DONE_PAID_CHECK
        if booking.receipt_file_id
        else texts.SCREEN_DONE_PAID_SPOT,
    )
    return (
        _compose(Screen.BOOK_DONE, ctx.cfg.club, body),
        footer_rows([("📋 Мои брони", nav(Screen.BOOKINGS))]),
    )


async def _render_privacy(ctx: ViewContext, _arg: str) -> tuple[str, InlineKeyboardMarkup]:
    from bot.keyboards.inline import CB_CONSENT_REVOKE

    return (
        _compose(Screen.PRIVACY, ctx.cfg.club, texts.render(texts.PRIVACY_TEXT, ctx.cfg.club)),
        footer_rows([("🚫 Отозвать согласие", CB_CONSENT_REVOKE)]),
    )


# --------------------------------------------------------------------------- #
# Админ-панель
# --------------------------------------------------------------------------- #
async def _render_admin(ctx: ViewContext, _arg: str) -> tuple[str, InlineKeyboardMarkup]:
    stats = await ctx.db.get_stats()
    summary = pre(
        table(
            ("ПОКАЗАТЕЛЬ", "ЗНАЧ"),
            [
                ("Заявок всего", stats.total),
                ("Ожидают", stats.pending),
                ("Одобрено", stats.approved),
                ("Отклонено", stats.rejected),
                ("Клиентов", stats.users),
                ("Администраторов", f"{stats.admins_active}/{stats.admins}"),
            ],
        )
    )
    body = "\n".join(
        [
            summary,
            "",
            texts.render(texts.SCREEN_ADMIN_BODY, ctx.cfg.club, now=now_text(ctx.cfg.timezone)),
        ]
    )
    rows: list[list[Button]] = [
        [("📋 Активные брони", nav(Screen.ADMIN_PENDING, 0))],
        [("👥 Администраторы", nav(Screen.ADMIN_ADMINS))],
        [(BTN_REFRESH, nav(Screen.ADMIN)), (BTN_BACK, nav(SCREEN_BACK))],
    ]
    return _compose(Screen.ADMIN, ctx.cfg.club, body), keyboard(*rows)


async def _render_admin_pending(ctx: ViewContext, arg: str) -> tuple[str, InlineKeyboardMarkup]:
    try:
        page = max(0, int(arg))
    except (TypeError, ValueError):
        page = 0

    total = await ctx.db.count_bookings(BookingStatus.PENDING)
    pages = max(1, (total + PENDING_PAGE_SIZE - 1) // PENDING_PAGE_SIZE)
    page = min(page, pages - 1)

    entries = await ctx.db.list_bookings_with_users(
        BookingStatus.PENDING,
        limit=PENDING_PAGE_SIZE,
        offset=page * PENDING_PAGE_SIZE,
        oldest_first=True,
    )

    if not entries:
        body = texts.SCREEN_ADMIN_PENDING_EMPTY
    else:
        today = datetime.now(ctx.cfg.timezone).date()
        body = "\n\n".join(
            texts.SCREEN_ADMIN_PENDING_ITEM.format(
                booking_id=booking.id,
                tariff=escape_html(booking.tariff),
                time_slot=escape_html(booking.time_slot),
                client_name=escape_html(booking.client_name),
                client_link=user.link if user is not None else f"ID <code>{booking.user_id}</code>",
                created=format_datetime(booking.created_at, ctx.cfg.timezone, "%d.%m %H:%M"),
                amount=_amount_text(booking.tariff, booking.time_slot, today),
            )
            for booking, user in entries
        )

    rows: list[list[Button]] = [
        [
            (f"✅ №{booking.id}", admin_approve_callback(booking.id)),
            (f"❌ №{booking.id}", admin_reject_callback(booking.id)),
        ]
        for booking, _user in entries
    ]
    if pages > 1:
        rows.append(
            [
                ("◀", nav(Screen.ADMIN_PENDING, max(0, page - 1))),
                (f"{page + 1}/{pages}", nav(Screen.ADMIN_PENDING, page)),
                ("▶", nav(Screen.ADMIN_PENDING, min(pages - 1, page + 1))),
            ]
        )
    rows.append([(BTN_REFRESH, nav(Screen.ADMIN_PENDING, page))])
    rows.append([(BTN_BACK, nav(SCREEN_BACK)), ("🏠 В меню", nav(Screen.MENU))])
    extra = f"стр. {page + 1}/{pages}" if pages > 1 else ""
    return _compose(Screen.ADMIN_PENDING, ctx.cfg.club, body, extra=extra), keyboard(*rows)


async def _render_admin_admins(ctx: ViewContext, _arg: str) -> tuple[str, InlineKeyboardMarkup]:
    admins = await ctx.db.list_admins()
    viewer_is_owner = await _is_owner(ctx)
    total, active = await ctx.db.count_admins()

    blocks = [
        texts.render(
            texts.SCREEN_ADMIN_ADMINS_BODY, ctx.cfg.club, admins_total=total, admins_active=active
        )
    ]
    if not admins:
        blocks.append(texts.ADMINS_EMPTY.strip())
    if not viewer_is_owner:
        viewer = await ctx.db.get_admin(ctx.user_id)
        role = viewer.role_title if viewer is not None else AdminRole.title(AdminRole.OPERATOR)
        blocks.append(texts.ADMIN_MANAGE_FORBIDDEN.format(role=role))

    rows: list[list[Button]] = []
    for admin in admins:
        label = await _admin_short_label(ctx, admin.user_id)
        mark = "👑" if admin.is_owner else "👤"
        state = "🟢" if admin.is_active else "🔴"
        rows.append([(f"{state} {mark} {label}", nav(Screen.ADMIN_ADMIN, admin.user_id))])
    if viewer_is_owner:
        rows.append([("➕ Добавить", CB_ADMIN_ADD)])
    rows.append([(BTN_REFRESH, nav(Screen.ADMIN_ADMINS))])
    rows.append([(BTN_BACK, nav(SCREEN_BACK)), ("🏠 В меню", nav(Screen.MENU))])
    return _compose(Screen.ADMIN_ADMINS, ctx.cfg.club, "\n\n".join(blocks)), keyboard(*rows)


async def _render_admin_card(ctx: ViewContext, arg: str) -> tuple[str, InlineKeyboardMarkup]:
    try:
        target_id = int(arg)
    except (TypeError, ValueError):
        return await _render_admin_admins(ctx, "")

    admin = await ctx.db.get_admin(target_id)
    if admin is None:
        return await _render_admin_admins(ctx, "")

    is_env_owner = admin.user_id == ctx.cfg.admin_id
    body = texts.render(
        texts.SCREEN_ADMIN_ADMIN_BODY,
        ctx.cfg.club,
        link=await _admin_label(ctx, admin.user_id),
        user_id=admin.user_id,
        role=admin.role_title,
        state=texts.ADMINS_STATE_ACTIVE if admin.is_active else texts.ADMINS_STATE_DISABLED,
        added_at=format_datetime(admin.created_at, ctx.cfg.timezone),
        added_by=await _admin_label(ctx, admin.added_by) if admin.added_by else "из .env",
    )
    if is_env_owner:
        body += f"\n\n{texts.ADMINS_OWNER_ENV_NOTE.strip()}"

    rows: list[list[Button]] = []
    if await _is_owner(ctx) and not is_env_owner:
        rows.append(
            [
                (
                    "⛔️ Отключить" if admin.is_active else "✅ Включить",
                    admin_toggle_callback(admin.user_id),
                )
            ]
        )
        role_row: list[Button] = []
        if admin.role != AdminRole.OWNER:
            role_row.append(("👑 Сделать владельцем", admin_promote_callback(admin.user_id)))
        if admin.role != AdminRole.OPERATOR:
            role_row.append(("👤 Сделать оператором", admin_demote_callback(admin.user_id)))
        if role_row:
            rows.append(role_row)
        rows.append([("🗑 Удалить", admin_delete_callback(admin.user_id))])

    rows.append([(BTN_REFRESH, nav(Screen.ADMIN_ADMIN, admin.user_id))])
    rows.append([("◀ К списку", nav(Screen.ADMIN_ADMINS)), ("🏠 В меню", nav(Screen.MENU))])
    return _compose(Screen.ADMIN_ADMIN, ctx.cfg.club, body), keyboard(*rows)


async def _render_admin_add(ctx: ViewContext, _arg: str) -> tuple[str, InlineKeyboardMarkup]:
    from bot.keyboards.inline import CB_ADMIN_ADD_CANCEL

    body = texts.render(texts.ADMIN_ADD_PROMPT, ctx.cfg.club)
    return (
        _compose(Screen.ADMIN_ADD, ctx.cfg.club, body),
        keyboard([(BTN_CANCEL, CB_ADMIN_ADD_CANCEL)]),
    )


def _amount_text(tariff_title: str, time_slot: str, today: Any) -> str:
    """Сумма заявки для списка администратора: «650 ₽» или «по прайсу»."""
    amount = texts.amount_for_booking(tariff_title, time_slot, today)
    return f"{amount} ₽" if amount is not None else "по прайсу"


# --- подписи администраторов ------------------------------------------------ #
async def _admin_label(ctx: ViewContext, user_id: int) -> str:
    """Как показать администратора: «@nickname» или «Имя (ID: …)»."""
    stored = await ctx.db.get_user(user_id)
    if stored is not None:
        return stored.link
    return await _label_from_telegram(ctx, user_id, short=False)


async def _admin_short_label(ctx: ViewContext, user_id: int) -> str:
    """Короткая подпись для кнопки: «@nickname» или «id123»."""
    stored = await ctx.db.get_user(user_id)
    if stored is not None:
        return stored.mention
    return await _label_from_telegram(ctx, user_id, short=True)


async def _label_from_telegram(ctx: ViewContext, user_id: int, *, short: bool) -> str:
    """Подпись из профиля Telegram (администратор мог ни разу не писать боту)."""
    try:
        chat = await ctx.bot.get_chat(user_id)
    except Exception:  # noqa: BLE001 — профиль недоступен, показываем ID
        logger.debug("Не удалось получить профиль администратора %s", user_id)
        return f"id{user_id}" if short else f"пользователь (ID: <code>{user_id}</code>)"
    username = getattr(chat, "username", None)
    if username:
        if short:
            return f"@{username}"
        return f'<a href="https://t.me/{escape_html(username)}">@{escape_html(username)}</a>'
    name = getattr(chat, "full_name", None) or getattr(chat, "first_name", None)
    if name:
        return str(name) if short else f"{escape_html(name)} (ID: <code>{user_id}</code>)"
    return f"id{user_id}" if short else f"пользователь (ID: <code>{user_id}</code>)"


# --------------------------------------------------------------------------- #
# Диспетчер экранов
# --------------------------------------------------------------------------- #
Renderer = Callable[[ViewContext, str], Awaitable[tuple[str, InlineKeyboardMarkup]]]

_RENDERERS: dict[str, Renderer] = {
    Screen.MENU: _render_menu,
    Screen.PRICES: _render_prices,
    Screen.RULES: _render_rules,
    Screen.FAQ: _render_faq_list,
    Screen.FAQ_ITEM: _render_faq_item,
    Screen.CONTACTS: _render_contacts,
    Screen.BOOKINGS: _render_bookings,
    Screen.BOOK_TARIFF: _render_book_tariff,
    Screen.BOOK_TIME: _render_book_time,
    Screen.BOOK_TIME_TEXT: _render_book_time_text,
    Screen.BOOK_NAME: _render_book_name,
    Screen.BOOK_PAY: _render_book_pay,
    Screen.BOOK_RECEIPT: _render_book_receipt,
    Screen.BOOK_DONE: _render_book_done,
    Screen.PRIVACY: _render_privacy,
    Screen.ADMIN: _render_admin,
    Screen.ADMIN_PENDING: _render_admin_pending,
    Screen.ADMIN_ADMINS: _render_admin_admins,
    Screen.ADMIN_ADMIN: _render_admin_card,
    Screen.ADMIN_ADD: _render_admin_add,
}


async def render_screen(ctx: ViewContext, screen: str, arg: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """Собрать экран: ``(текст, клавиатура)``. Неизвестный экран — меню."""
    renderer = _RENDERERS.get(screen)
    if renderer is None:
        logger.debug("Неизвестный экран %r — показываю меню", screen)
        return await _render_menu(ctx, "")
    return await renderer(ctx, arg)


__all__ = [
    "MESSAGE_LIMIT",
    "Renderer",
    "ViewContext",
    "render_screen",
]
