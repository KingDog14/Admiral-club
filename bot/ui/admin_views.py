"""Рендереры всех экранов расширенной админ-панели (блоки 1–9).

Каждый экран строится в рамках единого графического сообщения (HD-карточка
1280×720 на абстрактном фоне + подпись ≤ 1024 символов без ``<pre>`` +
сбалансированная по правилу 60–30–10 inline-клавиатура с видимыми эмодзи).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramAPIError

from bot import texts
from bot.db.models import Admin, AdminRole, Booking, BookingStatus, RejectReason, User
from bot.keyboards.inline import (
    CB_ADMIN_ADD,
    CB_ADMIN_ADD_CANCEL,
    CB_ADMIN_ASSIGN_BRANCH_PREFIX,
    CB_ADMIN_BRANCH_ADM_PREFIX,
    CB_ADMIN_CANCEL_CONFIRM_PREFIX,
    CB_ADMIN_CANCEL_REASON_PREFIX,
    CB_ADMIN_EDIT_PREFIX,
    CB_ADMIN_HANDOVER_PREFIX,
    CB_ADMIN_INVITE_PREFIX,
    CB_ADMIN_MANUAL_PREFIX,
    CB_ADMIN_NOSHOW_PREFIX,
    CB_ADMIN_NOTIFY_PREFIX,
    CB_ADMIN_RECEIPT_PREFIX,
    CB_ADMIN_REJECT_CONFIRM_PREFIX,
    CB_ADMIN_REJECT_REASON_PREFIX,
    CB_ADMIN_ROLE_PREFIX,
    CB_ADMIN_SCHED_PREFIX,
    CB_ADMIN_SETTINGS_PREFIX,
    CB_ADMIN_TARIFF_ADM_PREFIX,
    PENDING_PAGE_SIZE,
    admin_approve_callback,
    admin_delete_callback,
    admin_reject_callback,
    admin_toggle_callback,
)
from bot.services.club_config import admin_branch_scope, effective_club_info
from bot.services.schedule import WEEKDAY_SHORT, schedule_parameters
from bot.states import (
    DATA_ADMIN_MANUAL,
    DATA_ADMIN_REJECT_REASON,
)
from bot.ui.cards import (
    COL_RED,
    render_admin_dashboard_card,
    render_admin_pending_card,
    render_admin_team_card,
    render_info_card,
    render_ticket_card,
)
from bot.ui.palette import Surface, Tone, tone_rgba
from bot.ui.screens import (
    BTN_BACK,
    BTN_CANCEL,
    BTN_HOME,
    BTN_REFRESH,
    SCREEN_BACK,
    ButtonSpec,
    Screen,
    keyboard,
    nav,
    section,
    status_tag,
)
from bot.utils.formatting import (
    escape_html,
    format_datetime,
    now_text,
    today_date_text,
    truncate_html,
)

if TYPE_CHECKING:
    from bot.ui.render import ScreenView, ViewContext

logger = logging.getLogger(__name__)

CAPTION_LIMIT: int = 1024


def _compose(screen: str, club_name: str, body: str, extra: str = "") -> str:
    header = section(screen, club_name, extra=extra)
    return truncate_html(f"{header}\n\n{body}", CAPTION_LIMIT)


def _view(card: Any, caption: str, markup: Any) -> ScreenView:
    from bot.ui.render import ScreenView

    return ScreenView(card=card, caption=caption, reply_markup=markup)


async def _viewer_admin(ctx: ViewContext) -> Admin | None:
    return await ctx.db.get_admin(ctx.user_id)


async def _is_owner(ctx: ViewContext) -> bool:
    if ctx.admins is not None:
        return await ctx.admins.is_owner(ctx.user_id)
    adm = await ctx.db.get_admin(ctx.user_id)
    if adm is not None:
        return adm.is_owner
    total, _ = await ctx.db.count_admins()
    return total == 0 and ctx.user_id == ctx.cfg.admin_id


async def _can_act(ctx: ViewContext) -> bool:
    adm = await ctx.db.get_admin(ctx.user_id)
    if adm is not None:
        return adm.can_act
    return True


async def _fsm_data(ctx: ViewContext) -> dict[str, Any]:
    if ctx.state is None:
        return {}
    return await ctx.state.get_data()


async def _admin_label(
    ctx: ViewContext, user_id: int | None, *, short: bool = False
) -> str:
    if not user_id:
        return "—"
    adm = await ctx.db.get_admin(user_id)
    if adm is not None and adm.display_name:
        if short:
            return adm.display_name
        return f"<b>{escape_html(adm.display_name)}</b> (<code>{user_id}</code>)"
    stored = await ctx.db.get_user(user_id)
    if stored is not None:
        return stored.mention if short else stored.link
    try:
        chat = await ctx.bot.get_chat(user_id)
    except TelegramAPIError:
        return f"id{user_id}" if short else f"пользователь (ID: <code>{user_id}</code>)"
    username = getattr(chat, "username", None)
    if username:
        return (
            f"@{username}"
            if short
            else f'<a href="https://t.me/{escape_html(username)}">@{escape_html(username)}</a>'
        )
    name = getattr(chat, "full_name", None) or getattr(chat, "first_name", None)
    if name:
        return (
            str(name) if short else f"{escape_html(name)} (ID: <code>{user_id}</code>)"
        )
    return f"id{user_id}" if short else f"пользователь (ID: <code>{user_id}</code>)"


def _amount_label(booking: Booking, today: Any) -> str:
    if booking.quoted_price is not None:
        return f"{booking.quoted_price} ₽"
    amt = texts.amount_for_booking(booking.tariff, booking.time_slot, today)
    return f"{amt} ₽" if amt is not None else "по прайсу"


# --------------------------------------------------------------------------- #
# 1. Главный экран админ-панели — Очередь броней и разделы управления
# --------------------------------------------------------------------------- #
async def render_admin_home(ctx: ViewContext, _arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    viewer = await _viewer_admin(ctx)
    scope_branch = admin_branch_scope(viewer)
    stats = await ctx.db.get_stats(branch_id=scope_branch)
    now_str = now_text(ctx.cfg.timezone)

    scope_line = ""
    if scope_branch is not None:
        br = await ctx.db.get_branch(scope_branch)
        if br is not None:
            scope_line = f"\n🏢 Ваша точка: <b>{escape_html(br.name)}</b>"
    elif viewer is not None and viewer.is_viewer:
        scope_line = "\n👁 Режим: <b>наблюдатель (только просмотр, без действий)</b>"

    body = (
        texts.render(
            texts.SCREEN_ADMIN_BODY,
            club,
            now=now_str,
            **stats.as_dict(),
        )
        + f"\n📅 Сегодня: ✅ <b>{stats.approved_today}</b> подтв. · "
        f"🚫 <b>{stats.rejected_today}</b> откл. · "
        f"🛠 Создано админом: <b>{stats.manual}</b>"
        + (f" · 🚷 Неявок: <b>{stats.no_shows}</b>" if stats.no_shows else "")
        + scope_line
    )

    pending_label = (
        f"📋 Активные брони · Новые ({stats.pending})"
        if stats.pending
        else "📋 Активные брони (0)"
    )
    rows: list[Sequence[ButtonSpec]] = [
        [
            (
                pending_label,
                nav(Screen.ADMIN_PENDING, 0),
                Surface.HERO if stats.pending else Surface.ACCENT,
                Tone.MINT if stats.pending else Tone.SAPPHIRE,
            )
        ],
        [
            (
                f"✅ Подтверждённые ({stats.approved_today})",
                nav(Screen.ADMIN_APPROVED, 0),
                Surface.ACCENT,
                Tone.JADE,
            ),
            (
                f"🚫 Отклонённые ({stats.rejected_today})",
                nav(Screen.ADMIN_REJECTED, 0),
                Surface.ACCENT,
                Tone.AMBER,
            ),
        ],
    ]
    if await _can_act(ctx):
        rows.append(
            [
                (
                    "➕ Создать бронь",
                    f"{CB_ADMIN_MANUAL_PREFIX}start",
                    Surface.GLASS,
                    Tone.MINT,
                ),
                ("🗓 Расписание", nav(Screen.ADMIN_SCHEDULE), Surface.GLASS, Tone.CRYO),
            ]
        )
    else:
        rows.append(
            [
                (
                    "🗓 Расписание и слоты",
                    nav(Screen.ADMIN_SCHEDULE),
                    Surface.GLASS,
                    Tone.CRYO,
                )
            ]
        )

    rows.extend(
        [
            [
                ("💰 Тарифы", nav(Screen.ADMIN_TARIFFS), Surface.GLASS, Tone.AMBER),
                ("🏢 Точки", nav(Screen.ADMIN_BRANCHES), Surface.GLASS, Tone.INDIGO),
            ],
            [
                (
                    "👥 Администраторы",
                    nav(Screen.ADMIN_ADMINS),
                    Surface.GLASS,
                    Tone.AMETHYST,
                ),
                (
                    "🔔 Уведомления",
                    nav(Screen.ADMIN_NOTIFY),
                    Surface.GLASS,
                    Tone.TANGERINE,
                ),
            ],
            [
                (
                    "⚙️ Настройки клуба",
                    nav(Screen.ADMIN_SETTINGS),
                    Surface.GLASS,
                    Tone.SAPPHIRE,
                ),
                (BTN_REFRESH, nav(Screen.ADMIN), Surface.GLASS, Tone.SLATE),
            ],
            [
                (BTN_BACK, nav(SCREEN_BACK), Surface.GLASS, Tone.SLATE),
                (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
            ],
        ]
    )
    return _view(
        render_admin_dashboard_card(club, stats, now_str),
        _compose(Screen.ADMIN, club.name, body),
        keyboard(*rows),
    )


# --------------------------------------------------------------------------- #
# 1а. Очередь «Новые» (ждут подтверждения)
# --------------------------------------------------------------------------- #
async def render_admin_pending(ctx: ViewContext, arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    viewer = await _viewer_admin(ctx)
    scope_branch = admin_branch_scope(viewer)
    can_act = await _can_act(ctx)

    try:
        page = max(0, int(arg))
    except (TypeError, ValueError):
        page = 0

    total = await ctx.db.count_bookings(BookingStatus.PENDING, branch_id=scope_branch)
    pages = max(1, (total + PENDING_PAGE_SIZE - 1) // PENDING_PAGE_SIZE)
    page = min(page, pages - 1)

    entries = await ctx.db.list_bookings_with_users(
        BookingStatus.PENDING,
        limit=PENDING_PAGE_SIZE,
        offset=page * PENDING_PAGE_SIZE,
        oldest_first=True,
        branch_id=scope_branch,
    )

    if not entries:
        body = texts.SCREEN_ADMIN_PENDING_EMPTY
    else:
        today = datetime.now(ctx.cfg.timezone).date()
        lines: list[str] = []
        for booking, user in entries:
            item = texts.SCREEN_ADMIN_PENDING_ITEM.format(
                booking_id=booking.id,
                tariff=escape_html(booking.tariff),
                time_slot=escape_html(booking.time_slot),
                client_name=escape_html(booking.client_name),
                client_link=user.link
                if user is not None
                else (
                    escape_html(booking.guest_contact)
                    if booking.guest_contact
                    else f"ID <code>{booking.user_id}</code>"
                ),
                created=format_datetime(
                    booking.created_at, ctx.cfg.timezone, "%d.%m %H:%M"
                ),
                amount=_amount_label(booking, today),
            )
            if booking.guests_count > 1:
                item += f" · 👥 {booking.guests_count}"
            if booking.guest_comment:
                item += f"\n   💬 <i>{escape_html(booking.guest_comment[:60])}</i>"
            lines.append(item)
        body = "\n\n".join(lines)

    rows: list[Sequence[ButtonSpec]] = []
    for booking, _user in entries:
        rows.append(
            [
                (
                    f"🎫 Карточка №{booking.id} · {booking.client_name[:12]}",
                    nav(Screen.ADMIN_BOOKING, booking.id),
                    Surface.GLASS,
                    Tone.CRYO,
                )
            ]
        )
        if can_act:
            rows.append(
                [
                    (
                        f"✅ №{booking.id}",
                        admin_approve_callback(booking.id),
                        Surface.HERO,
                        Tone.MINT,
                    ),
                    (
                        f"❌ №{booking.id}",
                        admin_reject_callback(booking.id),
                        Surface.DANGER,
                        Tone.RUBY,
                    ),
                ]
            )

    if pages > 1:
        rows.append(
            [
                (
                    "⬅️",
                    nav(Screen.ADMIN_PENDING, max(0, page - 1)),
                    Surface.GLASS,
                    Tone.SLATE,
                ),
                (
                    f"📄 {page + 1}/{pages}",
                    nav(Screen.ADMIN_PENDING, page),
                    Surface.GLASS,
                    Tone.SLATE,
                ),
                (
                    "➡️",
                    nav(Screen.ADMIN_PENDING, min(pages - 1, page + 1)),
                    Surface.GLASS,
                    Tone.SLATE,
                ),
            ]
        )
    rows.append(
        [(BTN_REFRESH, nav(Screen.ADMIN_PENDING, page), Surface.GLASS, Tone.SLATE)]
    )
    rows.append(
        [
            (BTN_BACK, nav(SCREEN_BACK), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    extra = f"стр. {page + 1}/{pages}" if pages > 1 else ""
    return _view(
        render_admin_pending_card(
            club,
            entries,
            total_pending=total,
            page=page,
            pages=pages,
            tz=ctx.cfg.timezone,
        ),
        _compose(Screen.ADMIN_PENDING, club.name, body, extra=extra),
        keyboard(*rows),
    )


# --------------------------------------------------------------------------- #
# 1б. Очередь «Подтверждённые (сегодня)» — список с временем
# --------------------------------------------------------------------------- #
async def render_admin_approved(ctx: ViewContext, arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    viewer = await _viewer_admin(ctx)
    scope_branch = admin_branch_scope(viewer)
    today_iso = ctx.db.today_iso()
    today_human = today_date_text(ctx.cfg.timezone)

    try:
        page = max(0, int(arg))
    except (TypeError, ValueError):
        page = 0

    total = await ctx.db.count_bookings(
        BookingStatus.APPROVED, branch_id=scope_branch, for_date=today_iso
    )
    pages = max(1, (total + PENDING_PAGE_SIZE - 1) // PENDING_PAGE_SIZE)
    page = min(page, pages - 1)

    entries = await ctx.db.list_bookings_with_users(
        BookingStatus.APPROVED,
        limit=PENDING_PAGE_SIZE,
        offset=page * PENDING_PAGE_SIZE,
        oldest_first=True,
        branch_id=scope_branch,
        for_date=today_iso,
    )

    if not entries:
        body = f"📭 На сегодня (<b>{today_human}</b>) подтверждённых броней пока нет."
    else:
        items: list[str] = []
        for b, u in entries:
            seat = f" · {escape_html(b.seat_label)}" if b.seat_label else ""
            manual = " · 🛠 <i>админ</i>" if b.created_by_admin else ""
            who = (
                u.link
                if u is not None
                else escape_html(b.guest_contact or b.client_name)
            )
            items.append(
                f"<b>№{b.id}</b> · 🕒 <b>{escape_html(b.time_slot)}</b> · "
                f"<b>{escape_html(b.tariff)}</b>{seat}\n"
                f"   👤 {escape_html(b.client_name)} ({who}) · 👥 {b.guests_count} чел.{manual}"
            )
        body = "\n\n".join(items)

    card = render_info_card(
        club,
        cache_tag=f"adm_app_{today_iso}_{total}_{page}",
        badge=f"СЕГОДНЯ · {today_human}",
        title=f"Подтверждённые брони ({total})",
        subtitle="Нажмите на бронь, чтобы открыть карточку, изменить слот/ПК или отменить",
        highlights=[
            (
                f"№{b.id} · {b.time_slot[:18]}",
                f"{b.tariff} · {b.client_name} ({b.guests_count} чел.)",
            )
            for b, _ in entries[:4]
        ]
        or [("Список пуст", "Новые подтверждённые записи появятся здесь сразу.")],
        accent=tone_rgba(Tone.JADE),
    )

    rows: list[Sequence[ButtonSpec]] = [
        [
            (
                f"🎫 №{b.id} · {b.slot_start or b.time_slot[:11]} · {b.client_name[:12]}",
                nav(Screen.ADMIN_BOOKING, b.id),
                Surface.GLASS,
                Tone.JADE,
            )
        ]
        for b, _ in entries
    ]
    if pages > 1:
        rows.append(
            [
                (
                    "⬅️",
                    nav(Screen.ADMIN_APPROVED, max(0, page - 1)),
                    Surface.GLASS,
                    Tone.SLATE,
                ),
                (
                    f"📄 {page + 1}/{pages}",
                    nav(Screen.ADMIN_APPROVED, page),
                    Surface.GLASS,
                    Tone.SLATE,
                ),
                (
                    "➡️",
                    nav(Screen.ADMIN_APPROVED, min(pages - 1, page + 1)),
                    Surface.GLASS,
                    Tone.SLATE,
                ),
            ]
        )
    rows.append(
        [(BTN_REFRESH, nav(Screen.ADMIN_APPROVED, page), Surface.GLASS, Tone.SLATE)]
    )
    rows.append(
        [
            ("🛠 В панель", nav(Screen.ADMIN), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    return _view(
        card,
        _compose(Screen.ADMIN_APPROVED, club.name, body, extra=today_human),
        keyboard(*rows),
    )


# --------------------------------------------------------------------------- #
# 1в. Очередь «Отклонённые (сегодня, с причиной)»
# --------------------------------------------------------------------------- #
async def render_admin_rejected(ctx: ViewContext, arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    viewer = await _viewer_admin(ctx)
    scope_branch = admin_branch_scope(viewer)
    today_iso = ctx.db.today_iso()
    today_human = today_date_text(ctx.cfg.timezone)

    try:
        page = max(0, int(arg))
    except (TypeError, ValueError):
        page = 0

    total = await ctx.db.count_bookings(
        BookingStatus.REJECTED, branch_id=scope_branch, for_date=today_iso
    )
    pages = max(1, (total + PENDING_PAGE_SIZE - 1) // PENDING_PAGE_SIZE)
    page = min(page, pages - 1)

    entries = await ctx.db.list_bookings_with_users(
        BookingStatus.REJECTED,
        limit=PENDING_PAGE_SIZE,
        offset=page * PENDING_PAGE_SIZE,
        oldest_first=False,
        branch_id=scope_branch,
        for_date=today_iso,
    )

    if not entries:
        body = (
            f"✨ За сегодня (<b>{today_human}</b>) отклонённых и отменённых броней нет."
        )
    else:
        items: list[str] = []
        for b, u in entries:
            who = (
                u.link
                if u is not None
                else escape_html(b.guest_contact or b.client_name)
            )
            kind = "отменена" if b.was_approved else "отклонена"
            items.append(
                f"<b>№{b.id}</b> · {escape_html(b.tariff)} · {escape_html(b.time_slot)}\n"
                f"   👤 {escape_html(b.client_name)} ({who}) · 🏷 {kind}: <b>{escape_html(b.reason_label)}</b>"
            )
        body = "\n\n".join(items)

    card = render_info_card(
        club,
        cache_tag=f"adm_rej_{today_iso}_{total}_{page}",
        badge=f"ОТКАЗЫ И ОТМЕНЫ · {today_human}",
        title=f"Отклонённые сегодня ({total})",
        subtitle="Фиксируются причины: «нет мест», «гость не оплатил», «отменил сам», «не пришёл»",
        highlights=[
            (f"№{b.id} · {b.client_name[:14]}", f"{b.tariff} · {b.reason_label[:44]}")
            for b, _ in entries[:4]
        ]
        or [("Чисто", "Отказов и отмен за сегодняшний день нет.")],
        accent=tone_rgba(Tone.AMBER),
    )

    rows: list[Sequence[ButtonSpec]] = [
        [
            (
                f"🎫 №{b.id} · {b.client_name[:12]} ({RejectReason.title(b.reject_reason)[:14]})",
                nav(Screen.ADMIN_BOOKING, b.id),
                Surface.GLASS,
                Tone.AMBER,
            )
        ]
        for b, _ in entries
    ]
    if pages > 1:
        rows.append(
            [
                (
                    "⬅️",
                    nav(Screen.ADMIN_REJECTED, max(0, page - 1)),
                    Surface.GLASS,
                    Tone.SLATE,
                ),
                (
                    f"📄 {page + 1}/{pages}",
                    nav(Screen.ADMIN_REJECTED, page),
                    Surface.GLASS,
                    Tone.SLATE,
                ),
                (
                    "➡️",
                    nav(Screen.ADMIN_REJECTED, min(pages - 1, page + 1)),
                    Surface.GLASS,
                    Tone.SLATE,
                ),
            ]
        )
    rows.append(
        [(BTN_REFRESH, nav(Screen.ADMIN_REJECTED, page), Surface.GLASS, Tone.SLATE)]
    )
    rows.append(
        [
            ("🛠 В панель", nav(Screen.ADMIN), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    return _view(
        card,
        _compose(Screen.ADMIN_REJECTED, club.name, body, extra=today_human),
        keyboard(*rows),
    )


# --------------------------------------------------------------------------- #
# 1г & 2. Детальная карточка брони + действия (Подтвердить / Отклонить / Изменить / Отменить)
# --------------------------------------------------------------------------- #
async def render_admin_booking_card(ctx: ViewContext, arg: str) -> ScreenView:
    try:
        booking_id = int(arg)
    except (TypeError, ValueError):
        return await render_admin_home(ctx, "")

    booking = await ctx.db.get_booking(booking_id)
    if booking is None:
        return await render_admin_home(ctx, "")

    club, _ = await effective_club_info(ctx.db, ctx.cfg, branch_id=booking.branch_id)
    user: User | None = (
        await ctx.db.get_user(booking.user_id) if booking.user_id > 0 else None
    )
    branch = await ctx.db.get_branch(booking.branch_id)
    today = datetime.now(ctx.cfg.timezone).date()
    can_act = await _can_act(ctx)

    date_str = booking.booking_date or booking.created_at[:10]
    span_str = (
        f"{booking.slot_start}–{booking.slot_end}"
        if booking.slot_start and booking.slot_end
        else booking.time_slot
    )
    seat_str = (
        f" · <b>{escape_html(booking.seat_label)}</b>" if booking.seat_label else ""
    )
    guest_link = user.link if user is not None else "офлайн-гость"
    contact_str = (
        f" · 📞 <code>{escape_html(booking.guest_contact)}</code>"
        if booking.guest_contact
        else ""
    )

    receipt_status = "оплата на месте (без чека)"
    if booking.receipt_file_id:
        receipt_status = (
            "PDF-документ прикреплён"
            if booking.receipt_kind == "document"
            else "фото чека прикреплено"
        )

    lines = [
        f"<blockquote>🏷 Статус: {status_tag(booking.status)}"
        + (" · 🛠 <b>создана админом</b>" if booking.created_by_admin else "")
        + "</blockquote>",
        f"📅 Дата и время: <b>{escape_html(date_str)} · {escape_html(span_str)}</b> ({escape_html(booking.time_slot)})",
        f"👥 Количество человек: <b>{booking.guests_count}</b>",
        f"🖥 Зал / ПК: <b>{escape_html(booking.tariff)}</b>{seat_str}",
        f"👤 Гость: <b>{escape_html(booking.client_name)}</b> ({guest_link}){contact_str}",
        f"🧾 Чек: <b>{receipt_status}</b> · Сумма: <b>{_amount_label(booking, today)}</b>",
    ]
    if branch is not None:
        lines.append(
            f"🏢 Точка: <b>{escape_html(branch.name)}</b> ({escape_html(branch.address)})"
        )
    if booking.extras:
        extras_map = {
            e.key: e.title for e in await ctx.db.list_tariff_extras(active_only=False)
        }
        lines.append(
            "✨ Доп. услуги: <b>"
            + escape_html(", ".join(extras_map.get(k, k) for k in booking.extras))
            + "</b>"
        )
    if booking.guest_comment:
        lines.append(
            f"💬 Комментарий гостя: <i>{escape_html(booking.guest_comment)}</i>"
        )
    if booking.status == BookingStatus.REJECTED:
        lines.append(
            f"🚫 Причина отказа/отмены: <b>{escape_html(booking.reason_label)}</b>"
        )
    if booking.decided_by:
        decider = await _admin_label(ctx, booking.decided_by, short=True)
        lines.append(f"🧑‍💼 Обработал: {escape_html(decider)}")

    rows: list[Sequence[ButtonSpec]] = []
    if can_act:
        if booking.is_pending:
            rows.append(
                [
                    (
                        "✅ Подтвердить",
                        admin_approve_callback(booking.id),
                        Surface.HERO,
                        Tone.MINT,
                    ),
                    (
                        "❌ Отклонить",
                        nav(Screen.ADMIN_BOOKING_REJECT, booking.id),
                        Surface.DANGER,
                        Tone.RUBY,
                    ),
                ]
            )
            edit_row: list[ButtonSpec] = [
                (
                    "✏️ Изменить",
                    nav(Screen.ADMIN_BOOKING_EDIT, booking.id),
                    Surface.GLASS,
                    Tone.AMBER,
                ),
            ]
            if booking.receipt_file_id:
                edit_row.append(
                    (
                        "📎 Открыть чек",
                        f"{CB_ADMIN_RECEIPT_PREFIX}{booking.id}",
                        Surface.GLASS,
                        Tone.SAPPHIRE,
                    )
                )
            rows.append(edit_row)
        elif booking.is_approved:
            rows.append(
                [
                    (
                        "✏️ Изменить",
                        nav(Screen.ADMIN_BOOKING_EDIT, booking.id),
                        Surface.ACCENT,
                        Tone.AMBER,
                    )
                ]
            )
            rows.append(
                [
                    (
                        "🚫 Отменить",
                        nav(Screen.ADMIN_BOOKING_CANCEL, booking.id),
                        Surface.DANGER,
                        Tone.RUBY,
                    ),
                    (
                        "🚷 Не пришёл",
                        f"{CB_ADMIN_NOSHOW_PREFIX}{booking.id}",
                        Surface.DANGER,
                        Tone.TANGERINE,
                    ),
                ]
            )
            if booking.receipt_file_id:
                rows.append(
                    [
                        (
                            "📎 Открыть чек",
                            f"{CB_ADMIN_RECEIPT_PREFIX}{booking.id}",
                            Surface.GLASS,
                            Tone.SAPPHIRE,
                        )
                    ]
                )
        elif booking.receipt_file_id:
            rows.append(
                [
                    (
                        "📎 Открыть чек",
                        f"{CB_ADMIN_RECEIPT_PREFIX}{booking.id}",
                        Surface.GLASS,
                        Tone.SAPPHIRE,
                    )
                ]
            )
    elif booking.receipt_file_id:
        rows.append(
            [
                (
                    "📎 Открыть чек",
                    f"{CB_ADMIN_RECEIPT_PREFIX}{booking.id}",
                    Surface.GLASS,
                    Tone.SAPPHIRE,
                )
            ]
        )

    rows.append(
        [
            ("🛠 В панель", nav(Screen.ADMIN), Surface.GLASS, Tone.SLATE),
            (
                BTN_REFRESH,
                nav(Screen.ADMIN_BOOKING, booking.id),
                Surface.GLASS,
                Tone.SLATE,
            ),
        ]
    )
    rows.append(
        [
            (BTN_BACK, nav(SCREEN_BACK), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    return _view(
        render_ticket_card(club, booking),
        _compose(
            Screen.ADMIN_BOOKING, club.name, "\n".join(lines), extra=f"№{booking.id}"
        ),
        keyboard(*rows),
    )


async def render_admin_booking_reject(
    ctx: ViewContext, arg: str, *, cancel_mode: bool = False
) -> ScreenView:
    try:
        booking_id = int(arg)
    except (TypeError, ValueError):
        return await render_admin_home(ctx, "")

    booking = await ctx.db.get_booking(booking_id)
    if booking is None:
        return await render_admin_home(ctx, "")

    club, _ = await effective_club_info(ctx.db, ctx.cfg, branch_id=booking.branch_id)
    data = await _fsm_data(ctx)
    chosen_reason = data.get(DATA_ADMIN_REJECT_REASON)

    screen_code = (
        Screen.ADMIN_BOOKING_CANCEL if cancel_mode else Screen.ADMIN_BOOKING_REJECT
    )
    choices = (
        RejectReason.CANCEL_CHOICES if cancel_mode else RejectReason.REJECT_CHOICES
    )
    prefix = (
        CB_ADMIN_CANCEL_REASON_PREFIX if cancel_mode else CB_ADMIN_REJECT_REASON_PREFIX
    )
    confirm_prefix = (
        CB_ADMIN_CANCEL_CONFIRM_PREFIX
        if cancel_mode
        else CB_ADMIN_REJECT_CONFIRM_PREFIX
    )

    if chosen_reason:
        body = (
            f"Бронь <b>№{booking.id}</b> ({escape_html(booking.client_name)}, {escape_html(booking.time_slot)})\n"
            f"Выбрана причина: <b>{escape_html(RejectReason.title(str(chosen_reason)))}</b>.\n\n"
            "✍️ Напишите в чат <b>комментарий для гостя</b> (необязательно) или нажмите кнопку ниже — "
            "бот автоматически предложит гостю ближайшие свободные слоты."
        )
        rows: list[Sequence[ButtonSpec]] = [
            [
                (
                    "🚫 Подтвердить без комментария"
                    if cancel_mode
                    else "❌ Отклонить без комментария",
                    f"{confirm_prefix}{booking.id}",
                    Surface.DANGER,
                    Tone.RUBY,
                )
            ],
            [
                (
                    "🎫 Назад к брони",
                    nav(Screen.ADMIN_BOOKING, booking.id),
                    Surface.GLASS,
                    Tone.SLATE,
                )
            ],
        ]
    else:
        action_word = (
            "отмены подтверждённой брони" if cancel_mode else "отклонения заявки"
        )
        body = (
            f"Выберите причину {action_word} <b>№{booking.id}</b> "
            f"({escape_html(booking.tariff)} · {escape_html(booking.time_slot)}):\n\n"
            "После выбора причины можно добавить комментарий — гость получит вежливое уведомление "
            "и свободные альтернативные слоты."
        )
        rows = [
            [(label, f"{prefix}{booking.id}:{code}", Surface.GLASS, Tone.AMBER)]
            for code, label in choices
        ]
        rows.append(
            [
                (
                    "🎫 К карточке брони",
                    nav(Screen.ADMIN_BOOKING, booking.id),
                    Surface.GLASS,
                    Tone.SLATE,
                ),
                (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
            ]
        )

    card = render_info_card(
        club,
        cache_tag=f"adm_rej_flow_{booking.id}_{cancel_mode}_{chosen_reason}",
        badge="ОТМЕНА БРОНИ" if cancel_mode else "ОТКЛОНЕНИЕ ЗАЯВКИ",
        title=f"Бронь №{booking.id} · {booking.client_name}",
        subtitle="Укажите причину — гость получит альтернативные свободные окна",
        highlights=[
            ("Слот гостя", f"{booking.tariff} · {booking.time_slot}"),
            (
                "Причина",
                RejectReason.title(str(chosen_reason))
                if chosen_reason
                else "Выберите кнопкой ниже",
            ),
        ],
        accent=COL_RED,
    )
    return _view(card, _compose(screen_code, club.name, body), keyboard(*rows))


async def render_admin_booking_cancel(ctx: ViewContext, arg: str) -> ScreenView:
    return await render_admin_booking_reject(ctx, arg, cancel_mode=True)


async def render_admin_booking_edit(ctx: ViewContext, arg: str) -> ScreenView:
    try:
        booking_id = int(arg)
    except (TypeError, ValueError):
        return await render_admin_home(ctx, "")

    booking = await ctx.db.get_booking(booking_id)
    if booking is None:
        return await render_admin_home(ctx, "")

    club, _ = await effective_club_info(ctx.db, ctx.cfg, branch_id=booking.branch_id)
    tariffs = await ctx.db.list_tariffs(active_only=True)

    body = (
        f"✏️ <b>Изменение брони №{booking.id}</b> ({escape_html(booking.client_name)})\n\n"
        f"• Сейчас время: <b>{escape_html(booking.time_slot)}</b>\n"
        f"• Гостей: <b>{booking.guests_count}</b>\n"
        f"• Зал / ПК: <b>{escape_html(booking.tariff)}</b>"
        + (
            f" · <b>{escape_html(booking.seat_label)}</b>"
            if booking.seat_label
            else " · <i>ПК не назначен</i>"
        )
        + "\n\nВыберите, что изменить (гость автоматически получит уведомление «бронь изменена»):"
    )
    rows: list[Sequence[ButtonSpec]] = [
        [
            (
                "🕒 Изменить время",
                f"{CB_ADMIN_EDIT_PREFIX}{booking.id}:time",
                Surface.ACCENT,
                Tone.CRYO,
            ),
            (
                "🖥 Номер ПК",
                f"{CB_ADMIN_EDIT_PREFIX}{booking.id}:seat",
                Surface.ACCENT,
                Tone.SAPPHIRE,
            ),
        ],
        [
            (
                f"👥 {n} чел.",
                f"{CB_ADMIN_EDIT_PREFIX}{booking.id}:gset:{n}",
                Surface.GLASS,
                Tone.MINT,
            )
            for n in (1, 2, 3, 4)
        ],
        [
            (
                f"{t.emoji} {t.title}",
                f"{CB_ADMIN_EDIT_PREFIX}{booking.id}:tf:{t.key}",
                Surface.GLASS,
                Tone.AMBER,
            )
            for t in tariffs[:2]
        ],
        [
            (
                f"{t.emoji} {t.title}",
                f"{CB_ADMIN_EDIT_PREFIX}{booking.id}:tf:{t.key}",
                Surface.GLASS,
                Tone.INDIGO,
            )
            for t in tariffs[2:4]
        ],
        [
            (
                "🎫 К карточке брони",
                nav(Screen.ADMIN_BOOKING, booking.id),
                Surface.GLASS,
                Tone.SLATE,
            ),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ],
    ]
    card = render_info_card(
        club,
        cache_tag=f"adm_edit_{booking.id}_{booking.tariff}_{booking.time_slot}_{booking.guests_count}_{booking.seat_label}",
        badge=f"РЕДАКТИРОВАНИЕ · №{booking.id}",
        title="Изменить время, гостей или ПК",
        subtitle="После сохранения гость сразу получит обновлённые детали визита",
        highlights=[
            ("Время", booking.time_slot),
            (
                "Зал / ПК и гости",
                f"{booking.tariff} ({booking.seat_label or 'без № ПК'}) · {booking.guests_count} чел.",
            ),
        ],
        accent=tone_rgba(Tone.AMBER),
    )
    return _view(
        card, _compose(Screen.ADMIN_BOOKING_EDIT, club.name, body), keyboard(*rows)
    )


# --------------------------------------------------------------------------- #
# 3. Ручное создание брони за гостя (звонок / ВК / лично)
# --------------------------------------------------------------------------- #
async def render_admin_manual(ctx: ViewContext, _arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    data = await _fsm_data(ctx)
    draft: dict[str, Any] = dict(data.get(DATA_ADMIN_MANUAL) or {})
    branches = await ctx.db.list_branches(active_only=True)
    tariffs = await ctx.db.list_tariffs(active_only=True)

    step = str(draft.get("step") or "tariff")
    rows: list[Sequence[ButtonSpec]] = []

    if step == "branch" and len(branches) >= 2:
        body = "🏢 <b>Шаг 1 · Выберите точку клуба</b>, в которую обратился гость:"
        for br in branches:
            rows.append(
                [
                    (
                        f"📍 {br.name} ({br.city})",
                        f"{CB_ADMIN_MANUAL_PREFIX}br:{br.id}",
                        Surface.GLASS,
                        Tone.INDIGO,
                    )
                ]
            )
    elif step == "tariff":
        body = "🖥 <b>Выберите зал</b> для ручной записи гостя:"
        for t in tariffs:
            rows.append(
                [
                    (
                        f"{t.emoji} {t.title} · {t.hour1} ₽/ч",
                        f"{CB_ADMIN_MANUAL_PREFIX}tf:{t.key}",
                        Surface.GLASS,
                        Tone.MINT,
                    )
                ]
            )
    elif step == "time":
        body = (
            f"🕒 Зал: <b>{escape_html(str(draft.get('tariff_title') or ''))}</b>\n"
            "Выберите время сессии кнопкой или введите своё (например <code>28.10 19:00-22:00</code>):"
        )
        rows.extend(
            [
                [
                    (
                        "⚡️ Сейчас (с текущего часа)",
                        f"{CB_ADMIN_MANUAL_PREFIX}tm:now",
                        Surface.HERO,
                        Tone.MINT,
                    )
                ],
                [
                    (
                        "🌆 Сегодня 18:00–21:00",
                        f"{CB_ADMIN_MANUAL_PREFIX}tm:18_21",
                        Surface.GLASS,
                        Tone.CRYO,
                    ),
                    (
                        "🌃 Сегодня 20:00–23:00",
                        f"{CB_ADMIN_MANUAL_PREFIX}tm:20_23",
                        Surface.GLASS,
                        Tone.SAPPHIRE,
                    ),
                ],
                [
                    (
                        "🌙 На ночь (22:00–08:00)",
                        f"{CB_ADMIN_MANUAL_PREFIX}tm:night",
                        Surface.GLASS,
                        Tone.INDIGO,
                    ),
                    (
                        "✏️ Ввести дату/время",
                        f"{CB_ADMIN_MANUAL_PREFIX}tm:custom",
                        Surface.GLASS,
                        Tone.AMBER,
                    ),
                ],
            ]
        )
    elif step == "guests":
        body = (
            f"👥 Зал: <b>{escape_html(str(draft.get('tariff_title') or ''))}</b> · "
            f"Время: <b>{escape_html(str(draft.get('time_slot') or ''))}</b>\n\n"
            "Укажите <b>количество гостей</b>:"
        )
        rows.append(
            [
                (f"👤 {n}", f"{CB_ADMIN_MANUAL_PREFIX}g:{n}", Surface.GLASS, Tone.MINT)
                for n in (1, 2, 3, 4, 5)
            ]
        )
    else:
        body = (
            "✍️ <b>Финальный шаг · Данные гостя</b>\n\n"
            f"• Зал: <b>{escape_html(str(draft.get('tariff_title') or ''))}</b>\n"
            f"• Время: <b>{escape_html(str(draft.get('time_slot') or ''))}</b>\n"
            f"• Гостей: <b>{draft.get('guests_count', 1)}</b>\n\n"
            "Отправьте одним сообщением <b>Имя | телефон или @username | ПК (опц.)</b>\n"
            "Пример: <code>Дмитрий | +7 900 555-11-22 | ПК #4</code>"
        )

    rows.append(
        [
            (BTN_CANCEL, f"{CB_ADMIN_MANUAL_PREFIX}cancel", Surface.DANGER, Tone.RUBY),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    card = render_info_card(
        club,
        cache_tag=f"adm_manual_{step}_{draft.get('tariff_key')}_{draft.get('guests_count')}",
        badge="РУЧНАЯ ЗАПИСЬ · СТОЙКА / ЗВОНОК / ВК",
        title="Создать бронь за гостя",
        subtitle="Запись сразу получает статус подтверждённой и помечается «создана админом»",
        highlights=[
            (
                "Зал и слот",
                f"{draft.get('tariff_title') or 'выбор зала'} · {draft.get('time_slot') or 'выбор времени'}",
            ),
            ("Учёт в статистике", "Отображается отдельной строкой в сводке панели."),
        ],
        accent=tone_rgba(Tone.MINT),
    )
    return _view(card, _compose(Screen.ADMIN_MANUAL, club.name, body), keyboard(*rows))


# --------------------------------------------------------------------------- #
# 4. Расписание / слоты / буфер уборки / праздники / занятые слоты (Sheets/IZI)
# --------------------------------------------------------------------------- #
async def render_admin_schedule(ctx: ViewContext, _arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    days = await ctx.db.list_schedule_days()
    holidays = await ctx.db.list_holidays()
    busy = await ctx.db.list_external_busy_slots(slot_date=ctx.db.today_iso())
    step_min, max_guests, buffer_min, _ = await schedule_parameters(ctx.db)
    is_owner = await _is_owner(ctx)

    day_summary = " · ".join(
        f"{WEEKDAY_SHORT[d.weekday]}: {'выходной' if d.is_closed else f'{d.open_time}–{d.close_time}'}"
        for d in days
    )
    hol_summary = (
        ", ".join(
            f"{h.holiday_date} ({'закрыто' if h.is_closed else f'{h.open_time}–{h.close_time}'})"
            for h in holidays[:3]
        )
        if holidays
        else "нет особых дат"
    )
    busy_summary = (
        ", ".join(
            f"{s.start_time}–{s.end_time} ({s.seats_taken} м., {s.source})"
            for s in busy[:3]
        )
        if busy
        else "нет внешних блокировок на сегодня"
    )

    body = (
        f"<blockquote>⏱ Шаг слота: <b>{step_min} мин</b> · 👥 Макс. в слоте: <b>{max_guests} чел.</b>\n"
        f"🧹 Буфер на уборку: <b>{buffer_min} мин</b></blockquote>\n"
        f"📅 <b>По дням недели:</b> {escape_html(day_summary)}\n"
        f"🎉 <b>Праздники:</b> {escape_html(hol_summary)}\n"
        f"📊 <b>Sheets / IZI сегодня:</b> {escape_html(busy_summary)}"
    )

    rows: list[Sequence[ButtonSpec]] = []
    if is_owner:
        next_step = 30 if step_min == 60 else 60
        next_buf = {0: 10, 10: 15, 15: 20, 20: 0}.get(buffer_min, 15)
        rows.extend(
            [
                [
                    (
                        f"⏱ Шаг: {step_min}м → {next_step}м",
                        f"{CB_ADMIN_SCHED_PREFIX}step:{next_step}",
                        Surface.ACCENT,
                        Tone.CRYO,
                    ),
                    (
                        f"🧹 Буфер: {buffer_min}м → {next_buf}м",
                        f"{CB_ADMIN_SCHED_PREFIX}buf:{next_buf}",
                        Surface.ACCENT,
                        Tone.JADE,
                    ),
                ],
                [
                    (
                        f"👥 Лимит слота ({max_guests})",
                        f"{CB_ADMIN_SCHED_PREFIX}max",
                        Surface.GLASS,
                        Tone.MINT,
                    ),
                    (
                        "🎉 Праздничные часы",
                        f"{CB_ADMIN_SCHED_PREFIX}hol",
                        Surface.GLASS,
                        Tone.AMBER,
                    ),
                ],
                [
                    (
                        f"🗓 {WEEKDAY_SHORT[wd]}",
                        f"{CB_ADMIN_SCHED_PREFIX}day:{wd}",
                        Surface.GLASS,
                        Tone.SAPPHIRE,
                    )
                    for wd in range(4)
                ],
                [
                    (
                        f"🗓 {WEEKDAY_SHORT[wd]}",
                        f"{CB_ADMIN_SCHED_PREFIX}day:{wd}",
                        Surface.GLASS,
                        Tone.INDIGO,
                    )
                    for wd in range(4, 7)
                ],
                [
                    (
                        "📊 Блок слота (Sheets/IZI)",
                        f"{CB_ADMIN_SCHED_PREFIX}busy",
                        Surface.GLASS,
                        Tone.TANGERINE,
                    ),
                ],
            ]
        )
        if busy:
            rows.append(
                [
                    (
                        f"🗑 Снять блок {busy[-1].start_time}–{busy[-1].end_time}",
                        f"{CB_ADMIN_SCHED_PREFIX}del:busy:{busy[-1].id}",
                        Surface.DANGER,
                        Tone.RUBY,
                    )
                ]
            )

    rows.append(
        [
            ("🛠 В панель", nav(Screen.ADMIN), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    card = render_info_card(
        club,
        cache_tag=f"adm_sched_{step_min}_{max_guests}_{buffer_min}_{len(holidays)}_{len(busy)}",
        badge="РАСПИСАНИЕ · СЛОТЫ · БУФЕР",
        title="Рабочие часы и ёмкость слотов",
        subtitle="Бот автоматически блокирует нерабочие часы и занятые слоты",
        highlights=[
            (
                "Шаг и буфер",
                f"Шаг слота {step_min} мин · буфер между сессиями {buffer_min} мин.",
            ),
            (
                "Лимит и праздники",
                f"До {max_guests} гостей в слоте · праздников в базе: {len(holidays)}.",
            ),
        ],
        accent=tone_rgba(Tone.CRYO),
    )
    return _view(
        card, _compose(Screen.ADMIN_SCHEDULE, club.name, body), keyboard(*rows)
    )


# --------------------------------------------------------------------------- #
# 5. Тарифы (будни / выходные) и доп. услуги («свой ПК», «VIP-зал», «напиток»)
# --------------------------------------------------------------------------- #
async def render_admin_tariffs(ctx: ViewContext, _arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    tariffs = await ctx.db.list_tariffs(active_only=False)
    extras = await ctx.db.list_tariff_extras(active_only=False)
    is_owner = await _is_owner(ctx)

    t_lines = [
        f"{t.emoji} <b>{escape_html(t.title)}</b> — будни: {t.hour1}/{t.hour3}/{t.hour5} ₽, ночь {t.night} ₽ · "
        f"ПТ–ВС: {t.hour1_weekend}/{t.hour3_weekend}/{t.hour5_weekend} ₽, ночь {t.night_weekend} ₽"
        for t in tariffs
    ]
    e_lines = [
        f"{'🟢' if e.is_active else '⚪️'} {e.emoji} <b>{escape_html(e.title)}</b>: "
        + (f"+{e.price} ₽" if e.price else "бесплатно")
        for e in extras
    ]
    body = (
        "<b>Базовые тарифы (1ч / 3ч / 5ч / ночь):</b>\n"
        + "\n".join(t_lines)
        + "\n\n<b>Доп. услуги к брони (видны гостю до оплаты):</b>\n"
        + "\n".join(e_lines)
    )

    rows: list[Sequence[ButtonSpec]] = [
        [
            (
                f"{t.emoji} {t.title}",
                nav(Screen.ADMIN_TARIFF_ITEM, t.key),
                Surface.GLASS,
                Tone.AMBER,
            )
            for t in tariffs[:2]
        ],
        [
            (
                f"{t.emoji} {t.title}",
                nav(Screen.ADMIN_TARIFF_ITEM, t.key),
                Surface.GLASS,
                Tone.CRYO,
            )
            for t in tariffs[2:4]
        ],
    ]
    if is_owner:
        for e in extras[:4]:
            mark = "🟢" if e.is_active else "⚪️"
            rows.append(
                [
                    (
                        f"{mark} {e.emoji} {e.title} ({e.price} ₽)",
                        f"{CB_ADMIN_TARIFF_ADM_PREFIX}ext:tg:{e.key}",
                        Surface.GLASS,
                        Tone.MINT if e.is_active else Tone.SLATE,
                    ),
                    (
                        "✏️ Цена",
                        f"{CB_ADMIN_TARIFF_ADM_PREFIX}ext:ed:{e.key}",
                        Surface.GLASS,
                        Tone.SAPPHIRE,
                    ),
                ]
            )
        rows.append(
            [
                (
                    "➕ Новая доп. услуга",
                    f"{CB_ADMIN_TARIFF_ADM_PREFIX}ext:add",
                    Surface.HERO,
                    Tone.MINT,
                )
            ]
        )

    rows.append(
        [
            ("🛠 В панель", nav(Screen.ADMIN), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    card = render_info_card(
        club,
        cache_tag=f"adm_tariffs_{len(tariffs)}_{sum(t.hour1 + t.night_weekend for t in tariffs)}_{sum(e.price for e in extras if e.is_active)}",
        badge="ТАРИФНАЯ СЕТКА И ОПЦИИ",
        title="Тарифы по дням и доп. услуги",
        subtitle="Гость видит точную цену с учётом дня недели и опций до подтверждения",
        highlights=[
            ("Будни и ПТ–ВС", "Отдельные ставки для 1 часа, 3 часов, 5 часов и ночи."),
            ("Доп. опции", ", ".join(f"{e.title} (+{e.price} ₽)" for e in extras[:3])),
        ],
        accent=tone_rgba(Tone.AMBER),
    )
    return _view(card, _compose(Screen.ADMIN_TARIFFS, club.name, body), keyboard(*rows))


async def render_admin_tariff_item(ctx: ViewContext, arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    rec = await ctx.db.get_tariff_record(arg)
    if rec is None:
        return await render_admin_tariffs(ctx, "")

    is_owner = await _is_owner(ctx)
    body = (
        f"{rec.emoji} <b>Зал {escape_html(rec.title)}</b> ({rec.pcs or 2} ПК)\n\n"
        f"• <b>Будни (ПН–ЧТ):</b> 1ч — <b>{rec.hour1} ₽</b> · 3ч — <b>{rec.hour3} ₽</b> · "
        f"5ч — <b>{rec.hour5} ₽</b> · ночь — <b>{rec.night} ₽</b>\n"
        f"• <b>Выходные (ПТ–ВС):</b> 1ч — <b>{rec.hour1_weekend} ₽</b> · 3ч — <b>{rec.hour3_weekend} ₽</b> · "
        f"5ч — <b>{rec.hour5_weekend} ₽</b> · ночь — <b>{rec.night_weekend} ₽</b>\n"
        f"• Дневные пакеты: утро — <b>{rec.morning} ₽</b> · день — <b>{rec.day} ₽</b>"
    )
    rows: list[Sequence[ButtonSpec]] = []
    if is_owner:
        rows.extend(
            [
                [
                    (
                        "✏️ Цены будней",
                        f"{CB_ADMIN_TARIFF_ADM_PREFIX}ed:{rec.key}:wd",
                        Surface.ACCENT,
                        Tone.CRYO,
                    ),
                    (
                        "✏️ Цены выходных",
                        f"{CB_ADMIN_TARIFF_ADM_PREFIX}ed:{rec.key}:we",
                        Surface.ACCENT,
                        Tone.AMBER,
                    ),
                ],
                [
                    (
                        "☀️ Утро / День / ПК",
                        f"{CB_ADMIN_TARIFF_ADM_PREFIX}ed:{rec.key}:pack",
                        Surface.GLASS,
                        Tone.MINT,
                    ),
                ],
            ]
        )
    rows.append(
        [
            ("💰 К тарифам", nav(Screen.ADMIN_TARIFFS), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    card = render_info_card(
        club,
        cache_tag=f"adm_tf_{rec.key}_{rec.hour1}_{rec.hour1_weekend}_{rec.night}_{rec.night_weekend}",
        badge=f"ЗАЛ · {rec.title.upper()}",
        title=f"Настройка тарифа {rec.title}",
        subtitle="Изменения сразу применяются в прайсе и калькуляторе бронирования",
        highlights=[
            (
                "Будни",
                f"1ч {rec.hour1} · 3ч {rec.hour3} · 5ч {rec.hour5} · ночь {rec.night} ₽",
            ),
            (
                "Выходные",
                f"1ч {rec.hour1_weekend} · 3ч {rec.hour3_weekend} · 5ч {rec.hour5_weekend} · ночь {rec.night_weekend} ₽",
            ),
        ],
        accent=tone_rgba(Tone.AMBER),
    )
    return _view(
        card, _compose(Screen.ADMIN_TARIFF_ITEM, club.name, body), keyboard(*rows)
    )


# --------------------------------------------------------------------------- #
# 6. Точки сети (для сетей компьютерных клубов)
# --------------------------------------------------------------------------- #
async def render_admin_branches(ctx: ViewContext, _arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    branches = await ctx.db.list_branches(active_only=False)
    active_count = sum(1 for b in branches if b.is_active)
    all_admins = await ctx.db.list_admins(active_only=True)
    is_owner = await _is_owner(ctx)

    lines: list[str] = []
    for br in branches:
        assigned = [f"id{a.user_id}" for a in all_admins if a.branch_id == br.id]
        adm_txt = ", ".join(assigned) if assigned else "все владельцы"
        st = "🟢" if br.is_active else "🔴"
        lines.append(
            f"{st} <b>#{br.id} {escape_html(br.name)}</b> — {escape_html(br.full_address)}\n"
            f"   🕒 {escape_html(br.hours)} · 📞 {escape_html(br.phone or club.admin_phone)} · 👥 Админы: {adm_txt}"
        )

    rule_note = (
        "Активно <b>2+ точек</b> — бот спрашивает точку у гостя <b>первым шагом</b>!"
        if active_count >= 2
        else "Сейчас активна <b>1 точка</b> — лишний шаг выбора точки у гостя скрыт."
    )
    body = "\n\n".join(lines) + f"\n\n<blockquote>ℹ️ {rule_note}</blockquote>"

    rows: list[Sequence[ButtonSpec]] = [
        [
            (
                f"{'🟢' if br.is_active else '🔴'} {br.name}",
                nav(Screen.ADMIN_BRANCH_ITEM, br.id),
                Surface.GLASS,
                Tone.INDIGO if br.is_active else Tone.SLATE,
            )
        ]
        for br in branches
    ]
    if is_owner:
        rows.append(
            [
                (
                    "➕ Добавить точку",
                    f"{CB_ADMIN_BRANCH_ADM_PREFIX}add",
                    Surface.HERO,
                    Tone.MINT,
                )
            ]
        )
    rows.append(
        [
            ("🛠 В панель", nav(Screen.ADMIN), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    card = render_info_card(
        club,
        cache_tag=f"adm_branches_{len(branches)}_{active_count}",
        badge=f"СЕТЬ КЛУБОВ · АКТИВНО {active_count}",
        title="Точки сети и контакты",
        subtitle="При 2+ активных точках бот предлагает выбрать филиал в самом начале брони",
        highlights=[
            (br.name, f"{br.full_address} · {br.hours}") for br in branches[:3]
        ],
        accent=tone_rgba(Tone.INDIGO),
    )
    return _view(
        card, _compose(Screen.ADMIN_BRANCHES, club.name, body), keyboard(*rows)
    )


async def render_admin_branch_item(ctx: ViewContext, arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    try:
        branch_id = int(arg)
    except (TypeError, ValueError):
        return await render_admin_branches(ctx, "")

    br = await ctx.db.get_branch(branch_id)
    if br is None:
        return await render_admin_branches(ctx, "")

    is_owner = await _is_owner(ctx)
    assigned = await ctx.db.list_admins(branch_id=br.id)
    adm_labels = [await _admin_label(ctx, a.user_id, short=True) for a in assigned]
    body = (
        f"🏢 <b>{escape_html(br.name)}</b> ({'🟢 активна' if br.is_active else '🔴 скрыта'})\n\n"
        f"• Город и адрес: <b>{escape_html(br.full_address)}</b>\n"
        f"• Часы работы: <b>{escape_html(br.hours)}</b>\n"
        f"• Контакты: <b>{escape_html(br.phone or club.admin_phone)}</b>\n"
        f"• Закреплённые админы: <b>{escape_html(', '.join(adm_labels) or 'общий пул владельцев')}</b>"
    )
    rows: list[Sequence[ButtonSpec]] = []
    if is_owner:
        rows.append(
            [
                (
                    "✏️ Изменить данные",
                    f"{CB_ADMIN_BRANCH_ADM_PREFIX}ed:{br.id}",
                    Surface.ACCENT,
                    Tone.SAPPHIRE,
                ),
                (
                    "🔴 Скрыть точку" if br.is_active else "🟢 Включить точку",
                    f"{CB_ADMIN_BRANCH_ADM_PREFIX}tg:{br.id}",
                    Surface.GLASS,
                    Tone.AMBER if br.is_active else Tone.MINT,
                ),
            ]
        )
    rows.append(
        [
            ("🏢 К точкам", nav(Screen.ADMIN_BRANCHES), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    card = render_info_card(
        club,
        cache_tag=f"adm_br_item_{br.id}_{br.name}_{br.is_active}_{br.address}",
        badge=f"ФИЛИАЛ #{br.id}",
        title=br.name,
        subtitle=br.full_address,
        highlights=[
            ("Режим и связь", f"{br.hours} · {br.phone or club.admin_phone}"),
            ("Персонал точки", ", ".join(adm_labels) or "Владельцы сети"),
        ],
        accent=tone_rgba(Tone.INDIGO),
    )
    return _view(
        card, _compose(Screen.ADMIN_BRANCH_ITEM, club.name, body), keyboard(*rows)
    )


# --------------------------------------------------------------------------- #
# 7. Администраторы: роли owner / admin / viewer, инвайт-ссылка и передача точки
# --------------------------------------------------------------------------- #
async def render_admin_admins(ctx: ViewContext, _arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    admins = await ctx.db.list_admins()
    branches = {b.id: b.name for b in await ctx.db.list_branches()}
    viewer_is_owner = await _is_owner(ctx)
    total, active = await ctx.db.count_admins()

    blocks = [
        texts.render(
            texts.SCREEN_ADMIN_ADMINS_BODY,
            club,
            admins_total=total,
            admins_active=active,
        ),
        "Роли: 👑 <b>owner</b> (всё) · 🛠 <b>admin</b> (своя точка) · 👁 <b>viewer</b> (только просмотр).",
    ]
    if not viewer_is_owner:
        viewer = await ctx.db.get_admin(ctx.user_id)
        role = (
            viewer.role_title
            if viewer is not None
            else AdminRole.title(AdminRole.OPERATOR)
        )
        blocks.append(texts.ADMIN_MANAGE_FORBIDDEN.format(role=role))

    rows: list[Sequence[ButtonSpec]] = []
    for admin in admins:
        label = await _admin_label(ctx, admin.user_id, short=True)
        role_icon = "👑" if admin.is_owner else ("👁" if admin.is_viewer else "👤")
        state = "🟢" if admin.is_active else "🔴"
        br_suffix = (
            f" · {branches[admin.branch_id]}" if admin.branch_id in branches else ""
        )
        adm_tone = (
            Tone.RUBY
            if not admin.is_active
            else (
                Tone.AMBER
                if admin.is_owner
                else (Tone.INDIGO if admin.is_viewer else Tone.MINT)
            )
        )
        rows.append(
            [
                (
                    f"{state} {role_icon} {label}{br_suffix}",
                    nav(Screen.ADMIN_ADMIN, admin.user_id),
                    Surface.GLASS,
                    adm_tone,
                )
            ]
        )
    if viewer_is_owner:
        rows.append(
            [
                ("➕ Добавить", CB_ADMIN_ADD, Surface.HERO, Tone.MINT),
            ]
        )
        rows.append(
            [
                (
                    "🔗 Инвайт (admin)",
                    f"{CB_ADMIN_INVITE_PREFIX}operator",
                    Surface.GLASS,
                    Tone.CRYO,
                ),
                (
                    "🔗 Инвайт (viewer)",
                    f"{CB_ADMIN_INVITE_PREFIX}viewer",
                    Surface.GLASS,
                    Tone.INDIGO,
                ),
            ]
        )
    rows.append([(BTN_REFRESH, nav(Screen.ADMIN_ADMINS), Surface.GLASS, Tone.SLATE)])
    rows.append(
        [
            (BTN_BACK, nav(SCREEN_BACK), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    return _view(
        render_admin_team_card(club, admins),
        _compose(Screen.ADMIN_ADMINS, club.name, "\n\n".join(blocks)),
        keyboard(*rows),
    )


async def render_admin_card(ctx: ViewContext, arg: str) -> ScreenView:
    try:
        target_id = int(arg)
    except (TypeError, ValueError):
        return await render_admin_admins(ctx, "")

    admin = await ctx.db.get_admin(target_id)
    if admin is None:
        return await render_admin_admins(ctx, "")

    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    all_admins = await ctx.db.list_admins()
    branches = await ctx.db.list_branches(active_only=True)
    branch_obj = await ctx.db.get_branch(admin.branch_id) if admin.branch_id else None
    is_env_owner = admin.user_id == ctx.cfg.admin_id

    body = texts.render(
        texts.SCREEN_ADMIN_ADMIN_BODY,
        club,
        link=await _admin_label(ctx, admin.user_id),
        user_id=admin.user_id,
        role=admin.role_title,
        state=texts.ADMINS_STATE_ACTIVE
        if admin.is_active
        else texts.ADMINS_STATE_DISABLED,
        added_at=format_datetime(admin.created_at, ctx.cfg.timezone),
        added_by=await _admin_label(ctx, admin.added_by)
        if admin.added_by
        else "из .env",
    )
    body += f"\n🏢 Точка: <b>{escape_html(branch_obj.name) if branch_obj else 'все / основная'}</b>"
    if is_env_owner:
        body += f"\n\n{texts.ADMINS_OWNER_ENV_NOTE.strip()}"

    rows: list[Sequence[ButtonSpec]] = []
    if await _is_owner(ctx) and not is_env_owner:
        rows.append(
            [
                (
                    "⛔️ Отключить (деактивировать)"
                    if admin.is_active
                    else "✅ Включить обратно",
                    admin_toggle_callback(admin.user_id),
                    Surface.GLASS if admin.is_active else Surface.HERO,
                    Tone.AMBER if admin.is_active else Tone.MINT,
                )
            ]
        )
        role_buttons: list[ButtonSpec] = []
        if admin.role != AdminRole.OWNER:
            role_buttons.append(
                (
                    "👑 Сделать владельцем",
                    f"{CB_ADMIN_ROLE_PREFIX}owner:{admin.user_id}",
                    Surface.GLASS,
                    Tone.AMBER,
                )
            )
        if admin.role != AdminRole.OPERATOR:
            role_buttons.append(
                (
                    "👤 Сделать оператором",
                    f"{CB_ADMIN_ROLE_PREFIX}operator:{admin.user_id}",
                    Surface.GLASS,
                    Tone.SAPPHIRE,
                )
            )
        if admin.role != AdminRole.VIEWER:
            role_buttons.append(
                (
                    "👁 Наблюдатель (viewer)",
                    f"{CB_ADMIN_ROLE_PREFIX}viewer:{admin.user_id}",
                    Surface.GLASS,
                    Tone.INDIGO,
                )
            )
        if role_buttons:
            rows.append(role_buttons[:2])
            if len(role_buttons) > 2:
                rows.append(role_buttons[2:])

        # Привязка точки и передача смены
        next_br_id = 0
        if branches:
            ids = [b.id for b in branches]
            if admin.branch_id is None:
                next_br_id = ids[0]
            else:
                idx = ids.index(admin.branch_id) if admin.branch_id in ids else -1
                next_br_id = ids[idx + 1] if 0 <= idx < len(ids) - 1 else 0
        rows.append(
            [
                (
                    f"🏢 Точка: {branch_obj.name[:12] if branch_obj else 'Все'}",
                    f"{CB_ADMIN_ASSIGN_BRANCH_PREFIX}{next_br_id}:{admin.user_id}",
                    Surface.GLASS,
                    Tone.CRYO,
                ),
                (
                    "🔄 Передать точку",
                    nav(Screen.ADMIN_HANDOVER, admin.user_id),
                    Surface.GLASS,
                    Tone.JADE,
                ),
            ]
        )
        rows.append(
            [
                (
                    "🗑 Удалить",
                    admin_delete_callback(admin.user_id),
                    Surface.DANGER,
                    Tone.RUBY,
                )
            ]
        )

    rows.append(
        [
            (
                BTN_REFRESH,
                nav(Screen.ADMIN_ADMIN, admin.user_id),
                Surface.GLASS,
                Tone.SLATE,
            )
        ]
    )
    rows.append(
        [
            ("👥 К списку", nav(Screen.ADMIN_ADMINS), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    return _view(
        render_admin_team_card(club, all_admins, selected=admin),
        _compose(Screen.ADMIN_ADMIN, club.name, body),
        keyboard(*rows),
    )


async def render_admin_add(ctx: ViewContext, _arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    all_admins = await ctx.db.list_admins()
    body = (
        texts.render(texts.ADMIN_ADD_PROMPT, club)
        + "\n\n💡 Можно отправить <b>Имя и ID</b> через пробел (например: <code>Максим 555666777</code>) "
        "или выпустить одноразовую ссылку-приглашение на предыдущем экране."
    )
    return _view(
        render_admin_team_card(club, all_admins, adding=True),
        _compose(Screen.ADMIN_ADD, club.name, body),
        keyboard([(BTN_CANCEL, CB_ADMIN_ADD_CANCEL, Surface.DANGER, Tone.RUBY)]),
    )


async def render_admin_handover(ctx: ViewContext, arg: str) -> ScreenView:
    try:
        from_uid = int(arg)
    except (TypeError, ValueError):
        return await render_admin_admins(ctx, "")

    source = await ctx.db.get_admin(from_uid)
    if source is None:
        return await render_admin_admins(ctx, "")

    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    peers = [
        a for a in await ctx.db.list_admins() if a.user_id != from_uid and a.is_active
    ]
    branch = await ctx.db.get_branch(source.branch_id or 1)
    br_name = branch.name if branch is not None else "Основная точка"

    body = (
        f"🔄 <b>Передача точки «{escape_html(br_name)}»</b>\n"
        f"Сдаёт смену: {await _admin_label(ctx, from_uid)}\n\n"
        "Выберите администратора, которому передаётся точка (история всех решений сохраняется в базе):"
    )
    rows: list[Sequence[ButtonSpec]] = []
    for peer in peers:
        lbl = await _admin_label(ctx, peer.user_id, short=True)
        rows.append(
            [
                (
                    f"🤝 Передать → {lbl}",
                    f"{CB_ADMIN_HANDOVER_PREFIX}{from_uid}:{peer.user_id}",
                    Surface.GLASS,
                    Tone.MINT,
                )
            ]
        )
    rows.append(
        [
            (
                "🧑‍💼 Назад к карточке",
                nav(Screen.ADMIN_ADMIN, from_uid),
                Surface.GLASS,
                Tone.SLATE,
            ),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    card = render_info_card(
        club,
        cache_tag=f"adm_handover_{from_uid}_{len(peers)}",
        badge="СМЕНА ПЕРСОНАЛА",
        title=f"Передать точку «{br_name}»",
        subtitle="Закрепление точки переносится мгновенно без потери истории в БД",
        highlights=[
            ("Точка", br_name),
            ("Получателей доступно", str(len(peers))),
        ],
        accent=tone_rgba(Tone.JADE),
    )
    return _view(
        card, _compose(Screen.ADMIN_HANDOVER, club.name, body), keyboard(*rows)
    )


# --------------------------------------------------------------------------- #
# 8. Уведомления админу (типы пушей + тихие часы «не будить ночью»)
# --------------------------------------------------------------------------- #
async def render_admin_notify(ctx: ViewContext, _arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    adm = await ctx.db.get_admin(ctx.user_id)
    if adm is None:
        adm = Admin(
            user_id=ctx.user_id,
            role=AdminRole.OWNER
            if ctx.user_id == ctx.cfg.admin_id
            else AdminRole.OPERATOR,
        )

    quiet_str = (
        f"🌙 <b>{adm.quiet_start}–{adm.quiet_end}</b> (ночью не беспокоить)"
        if adm.quiet_start and adm.quiet_end
        else "☀️ <b>Круглосуточно 24/7</b> (без тихих часов)"
    )
    body = (
        "Персональные настройки пуш-уведомлений в личку:\n\n"
        f"• Новая бронь гостя: <b>{'🔔 Вкл' if adm.notify_new else '🔕 Выкл'}</b>\n"
        f"• Отмена брони гостем: <b>{'🔔 Вкл' if adm.notify_cancel else '🔕 Выкл'}</b>\n"
        f"• Отметка «гость не пришёл»: <b>{'🔔 Вкл' if adm.notify_noshow else '🔕 Выкл'}</b>\n"
        f"• Режим тишины: {quiet_str}"
    )

    rows: list[Sequence[ButtonSpec]] = [
        [
            (
                f"{'🔔' if adm.notify_new else '🔕'} Новые брони",
                f"{CB_ADMIN_NOTIFY_PREFIX}tg:new",
                Surface.ACCENT if adm.notify_new else Surface.GLASS,
                Tone.MINT if adm.notify_new else Tone.SLATE,
            ),
            (
                f"{'🔔' if adm.notify_cancel else '🔕'} Отмены гостем",
                f"{CB_ADMIN_NOTIFY_PREFIX}tg:cancel",
                Surface.ACCENT if adm.notify_cancel else Surface.GLASS,
                Tone.AMBER if adm.notify_cancel else Tone.SLATE,
            ),
        ],
        [
            (
                f"{'🔔' if adm.notify_noshow else '🔕'} Пуш «Гость не пришёл»",
                f"{CB_ADMIN_NOTIFY_PREFIX}tg:noshow",
                Surface.GLASS,
                Tone.TANGERINE if adm.notify_noshow else Tone.SLATE,
            )
        ],
        [
            (
                "🌙 Не будить 23:00–08:00",
                f"{CB_ADMIN_NOTIFY_PREFIX}q:23_08",
                Surface.GLASS,
                Tone.INDIGO,
            ),
            (
                "🌙 Не будить 00:00–09:00",
                f"{CB_ADMIN_NOTIFY_PREFIX}q:00_09",
                Surface.GLASS,
                Tone.AMETHYST,
            ),
        ],
        [
            (
                "✏️ Свои тихие часы",
                f"{CB_ADMIN_NOTIFY_PREFIX}q:custom",
                Surface.GLASS,
                Tone.CRYO,
            ),
            (
                "☀️ Присылать 24/7",
                f"{CB_ADMIN_NOTIFY_PREFIX}q:off",
                Surface.GLASS,
                Tone.JADE,
            ),
        ],
        [
            ("🛠 В панель", nav(Screen.ADMIN), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ],
    ]
    card = render_info_card(
        club,
        cache_tag=f"adm_ntf_{adm.user_id}_{adm.notify_new}_{adm.notify_cancel}_{adm.notify_noshow}_{adm.quiet_start}_{adm.quiet_end}",
        badge="ПУШ-УВЕДОМЛЕНИЯ И ТИХИЙ РЕЖИМ",
        title="Настройка уведомлений",
        subtitle="Выберите события для пушей и ночное окно тишины",
        highlights=[
            (
                "Активные пуши",
                f"Новые: {'да' if adm.notify_new else 'нет'} · Отмены: {'да' if adm.notify_cancel else 'нет'} · Неявки: {'да' if adm.notify_noshow else 'нет'}",
            ),
            (
                "Тихие часы",
                f"{adm.quiet_start}–{adm.quiet_end}"
                if adm.quiet_start
                else "Отключены (24/7)",
            ),
        ],
        accent=tone_rgba(Tone.TANGERINE),
    )
    return _view(card, _compose(Screen.ADMIN_NOTIFY, club.name, body), keyboard(*rows))


# --------------------------------------------------------------------------- #
# 9. Настройки клуба (название/адрес, чат, реквизиты текст/фото, приветствие, правила)
# --------------------------------------------------------------------------- #
async def render_admin_settings(ctx: ViewContext, _arg: str) -> ScreenView:
    club, settings = await effective_club_info(ctx.db, ctx.cfg)
    is_owner = await _is_owner(ctx)

    chat_url = settings.get("chat_url") or "не задана"
    welcome = settings.get("welcome_text") or "стандартное приветствие клуба"
    has_qr = bool(settings.get("payment_photo_file_id"))
    lead_h = settings.get("booking_rules_lead_hours") or "0"
    allow_cancel = settings.get("booking_rules_allow_cancel") != "0"
    penalty = settings.get("booking_rules_noshow_penalty") or "—"

    body = (
        f"• Клуб: <b>{escape_html(club.name)}</b> (г. {escape_html(club.city)}, {escape_html(club.address)})\n"
        f"• Общий чат гостей: <b>{escape_html(chat_url)}</b>\n"
        f"• Реквизиты: <code>{escape_html(club.payment_card)}</code>"
        + (" · 🖼 <b>фото/QR загружено</b>" if has_qr else "")
        + f"\n• Приветствие: <i>{escape_html(welcome[:70])}</i>\n"
        f"• Правила брони: за <b>{escape_html(lead_h)} ч</b> · "
        f"отмена гостем: <b>{'разрешена' if allow_cancel else 'запрещена'}</b>\n"
        f"• Неявка: <i>{escape_html(penalty[:90])}</i>"
    )

    rows: list[Sequence[ButtonSpec]] = []
    if is_owner:
        rows.extend(
            [
                [
                    (
                        "🏷 Название и адрес",
                        f"{CB_ADMIN_SETTINGS_PREFIX}ed:identity",
                        Surface.ACCENT,
                        Tone.SAPPHIRE,
                    ),
                    (
                        "💬 Ссылка на чат",
                        f"{CB_ADMIN_SETTINGS_PREFIX}ed:chat_url",
                        Surface.ACCENT,
                        Tone.CRYO,
                    ),
                ],
                [
                    (
                        "💳 Реквизиты (текст/фото)",
                        f"{CB_ADMIN_SETTINGS_PREFIX}ed:payment",
                        Surface.GLASS,
                        Tone.MINT,
                    ),
                    (
                        "👋 Текст приветствия",
                        f"{CB_ADMIN_SETTINGS_PREFIX}ed:welcome",
                        Surface.GLASS,
                        Tone.AMBER,
                    ),
                ],
                [
                    (
                        f"⏱ Горизонт ({lead_h} ч)",
                        f"{CB_ADMIN_SETTINGS_PREFIX}ed:lead_hours",
                        Surface.GLASS,
                        Tone.INDIGO,
                    ),
                    (
                        f"{'🟢' if allow_cancel else '🔴'} Отмена гостем",
                        f"{CB_ADMIN_SETTINGS_PREFIX}tg:cancel",
                        Surface.GLASS,
                        Tone.JADE if allow_cancel else Tone.TANGERINE,
                    ),
                ],
                [
                    (
                        "⚠️ Штраф / правило неявки",
                        f"{CB_ADMIN_SETTINGS_PREFIX}ed:noshow",
                        Surface.GLASS,
                        Tone.AMETHYST,
                    ),
                ],
            ]
        )

    rows.append(
        [
            ("🛠 В панель", nav(Screen.ADMIN), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    card = render_info_card(
        club,
        cache_tag=f"adm_set_{club.name}_{club.address}_{chat_url}_{has_qr}_{lead_h}_{allow_cancel}",
        badge="КОНФИГУРАЦИЯ КЛУБА",
        title="Настройки клуба и правила брони",
        subtitle="Все изменения сохраняются в SQLite и применяются без перезапуска бота",
        highlights=[
            ("Клуб и чат", f"{club.name} ({club.city}) · чат: {chat_url[:30]}"),
            (
                "Бронь и неявка",
                f"Запись за {lead_h} ч · отмена гостем: {'да' if allow_cancel else 'нет'}",
            ),
        ],
        accent=tone_rgba(Tone.SAPPHIRE),
    )
    return _view(
        card, _compose(Screen.ADMIN_SETTINGS, club.name, body), keyboard(*rows)
    )


__all__ = [
    "render_admin_add",
    "render_admin_admins",
    "render_admin_approved",
    "render_admin_booking_cancel",
    "render_admin_booking_card",
    "render_admin_booking_edit",
    "render_admin_booking_reject",
    "render_admin_branch_item",
    "render_admin_branches",
    "render_admin_card",
    "render_admin_handover",
    "render_admin_home",
    "render_admin_manual",
    "render_admin_notify",
    "render_admin_pending",
    "render_admin_rejected",
    "render_admin_schedule",
    "render_admin_settings",
    "render_admin_tariff_item",
    "render_admin_tariffs",
]
