"""Сборка экранов: графическая карточка + подпись + inline-клавиатура.

Каждый экран возвращает :class:`ScreenView`:

* ``card``         — HD-карточка 1280×720 (:class:`bot.ui.cards.RenderedCard`);
* ``caption``      — компактная HTML-подпись (до 1024 символов, без ``<pre>``);
* ``reply_markup`` — inline-клавиатура с цветными акцентами и ``CopyTextButton``.

В этом модуле нет вызовов Telegram API — отправку и редактирование на месте
выполняет :class:`bot.ui.navigator.Navigator`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterator, Sequence
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
    CB_ADMIN_ADD_CANCEL,
    CB_BOOKING_BACK,
    CB_BOOKING_CANCEL,
    CB_CONSENT_REVOKE,
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
    consent_keyboard,
    revoke_confirm_keyboard,
)
from bot.services.admins import AdminRegistry
from bot.states import DATA_CLIENT_NAME, DATA_TARIFF_KEY, DATA_TIME_KEY, DATA_TIME_SLOT
from bot.ui.cards import (
    COL_RED,
    RenderedCard,
    clean_card_text,
    render_admin_dashboard_card,
    render_admin_pending_card,
    render_admin_team_card,
    render_bookings_list_card,
    render_contacts_card,
    render_info_card,
    render_menu_card,
    render_prices_card,
    render_ticket_card,
    render_wizard_card,
)
from bot.ui.palette import (
    FAQ_TONES,
    TARIFF_TONES,
    TIME_TONES,
    Surface,
    Tone,
    tone_rgba,
)
from bot.ui.screens import (
    BTN_BACK,
    BTN_BOOK_HERO,
    BTN_CANCEL,
    BTN_HOME,
    BTN_REFRESH,
    BTN_STEP_BACK,
    SCREEN_BACK,
    ButtonSpec,
    Screen,
    btn,
    faq_answer_keyboard,
    faq_list_keyboard,
    footer_rows,
    keyboard,
    menu_keyboard,
    nav,
    rules_tabs_keyboard,
    section,
    status_tag,
)
from bot.utils.formatting import (
    escape_html,
    format_datetime,
    now_full_text,
    now_text,
    today_date_text,
    truncate_html,
)

logger = logging.getLogger(__name__)

#: Жёсткий лимит Telegram на длину подписи к фото/медиа.
CAPTION_LIMIT: int = 1024
#: Совместимость со старым именем константы.
MESSAGE_LIMIT: int = CAPTION_LIMIT


@dataclass(frozen=True, slots=True)
class ScreenView:
    """Полное представление экрана: карточка, подпись и клавиатура."""

    card: RenderedCard
    caption: str
    reply_markup: InlineKeyboardMarkup

    def __iter__(self) -> Iterator[Any]:
        """Совместимость с распаковкой ``caption, markup = view``."""
        yield self.caption
        yield self.reply_markup


@dataclass(slots=True)
class ViewContext:
    """Зависимости рендерера экрана: база, конфиг, бот и текущий пользователь."""

    db: Database
    cfg: AppConfig
    bot: Bot
    user_id: int
    state: FSMContext | None = None
    admins: AdminRegistry | None = None
    user: TelegramUser | None = None


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #
def _compose(screen: str, club: Any, body: str, extra: str = "") -> str:
    """Собрать аккуратную подпись под карточкой в пределах 1024 символов."""
    header = section(screen, club.name, extra=extra)
    return truncate_html(f"{header}\n\n{body}", CAPTION_LIMIT)


def _strip_html(text: str) -> str:
    return clean_card_text(re.sub(r"<[^>]+>", "", text))


async def _is_admin(ctx: ViewContext) -> bool:
    if ctx.admins is not None:
        return await ctx.admins.is_admin(ctx.user_id)
    if await ctx.db.is_active_admin(ctx.user_id):
        return True
    total, _ = await ctx.db.count_admins()
    if total == 0:
        return ctx.cfg.is_env_admin(ctx.user_id)
    return False


async def _admin_role_title(ctx: ViewContext) -> str | None:
    if not await _is_admin(ctx):
        return None
    record = await ctx.db.get_admin(ctx.user_id)
    if record is not None:
        return record.role_title
    if ctx.user_id == ctx.cfg.admin_id:
        return AdminRole.title(AdminRole.OWNER)
    return AdminRole.title(AdminRole.OPERATOR)


async def _is_owner(ctx: ViewContext) -> bool:
    if ctx.admins is not None:
        return await ctx.admins.is_owner(ctx.user_id)
    admin = await ctx.db.get_admin(ctx.user_id)
    if admin is not None:
        return admin.is_owner
    total, _ = await ctx.db.count_admins()
    return total == 0 and ctx.user_id == ctx.cfg.admin_id


async def _fsm(ctx: ViewContext) -> dict[str, Any]:
    if ctx.state is None:
        return {}
    return await ctx.state.get_data()


def _with_step_nav(*rows: Sequence[ButtonSpec]) -> InlineKeyboardMarkup:
    """Клавиатура шага мастера: кнопки шага + спокойный «‹ Шаг назад» и «✕ Отменить»."""
    prepared: list[Sequence[ButtonSpec]] = list(rows)
    prepared.append(
        [
            (BTN_STEP_BACK, CB_BOOKING_BACK, Surface.GLASS, Tone.SLATE),
            (BTN_CANCEL, CB_BOOKING_CANCEL, Surface.DANGER, Tone.RUBY),
        ]
    )
    return keyboard(*prepared)


def _pairs(items: Sequence[ButtonSpec], per_row: int = 2) -> list[list[ButtonSpec]]:
    flat = list(items)
    return [flat[i : i + per_row] for i in range(0, len(flat), per_row)]


# --------------------------------------------------------------------------- #
# Клиентские экраны
# --------------------------------------------------------------------------- #
async def _render_menu(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    role_title = await _admin_role_title(ctx)
    is_admin = role_title is not None
    card = render_menu_card(club, is_admin=is_admin, role_title=role_title)
    body = texts.render(texts.SCREEN_MENU_BODY, club)
    if is_admin:
        body += f"\n🛡 Доступ администратора активен (<b>{escape_html(role_title)}</b>)."
    return ScreenView(
        card=card,
        caption=_compose(Screen.MENU, club, body),
        reply_markup=menu_keyboard(is_admin),
    )


async def _render_prices(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    night_weekend = " · ".join(f"{t.title} {t.night_weekend} ₽" for t in texts.TARIFFS)
    body = "\n\n".join(
        [
            texts.render(
                texts.SCREEN_PRICES_BODY,
                club,
                morning_slot=texts.MORNING_SLOT,
                day_slot=texts.DAY_SLOT,
                night_slot=texts.NIGHT_SLOT,
            ),
            texts.render(texts.SCREEN_PRICES_NOTE, club, night_weekend=night_weekend),
        ]
    )
    return ScreenView(
        card=render_prices_card(club),
        caption=_compose(Screen.PRICES, club, body),
        reply_markup=footer_rows(
            [(BTN_BOOK_HERO, nav(Screen.BOOK_TARIFF), Surface.HERO, Tone.MINT)],
            [
                ("💡 Частые вопросы", nav(Screen.FAQ), Surface.GLASS, Tone.AMBER),
                ("📘 Правила", nav(Screen.RULES), Surface.GLASS, Tone.INDIGO),
            ],
        ),
    )


async def _render_rules(ctx: ViewContext, arg: str) -> ScreenView:
    club = ctx.cfg.club
    tab = (
        arg
        if arg in (texts.RULES_TAB_MAIN, texts.RULES_TAB_PAY, texts.RULES_TAB_CLUB)
        else texts.RULES_TAB_MAIN
    )

    if tab == texts.RULES_TAB_PAY:
        extra = "оплата и безопасность"
        template = texts.SCREEN_RULES_PAY
        card = render_info_card(
            club,
            cache_tag="rules_pay",
            badge="ПРАВИЛА · ОПЛАТА",
            title="Оплата и защита от мошенников",
            subtitle=f"Единый номер клуба: {club.payment_system} {club.payment_card} ({club.payment_holder})",
            highlights=[
                ("Предоплата", "Берётся только на ночные пакеты и только в ПТ–ВС."),
                (
                    "Оплата на месте",
                    "В будни и на утренних/дневных пакетах платите при входе.",
                ),
                (
                    "Только общая группа",
                    "Администраторы никогда не просят перевод в личке.",
                ),
                (
                    "Проверка по звонку",
                    f"Любые сомнения — звоните в клуб: {club.admin_phone}.",
                ),
            ],
            accent=tone_rgba(Tone.AMBER),
        )
    elif tab == texts.RULES_TAB_CLUB:
        extra = "порядок в зале"
        template = texts.SCREEN_RULES_CLUB
        card = render_info_card(
            club,
            cache_tag="rules_club",
            badge="ПРАВИЛА · ЗАЛ",
            title="Порядок в клубе и техника",
            subtitle="Комфортная атмосфера и исправное железо для каждого гостя",
            highlights=[
                (
                    "Чистая зона",
                    "Без алкоголя, курения, вейпов, электронных сигарет и агрессии.",
                ),
                (
                    "Настройки ПК",
                    "Стороннее ПО и подключение флешек — только через админа.",
                ),
                (
                    "Ваши аккаунты",
                    "Не забывайте выходить из Steam и FACEIT в конце сессии.",
                ),
                ("Оборудование", "При любом сбое сразу позовите администратора смены."),
            ],
            accent=tone_rgba(Tone.CRYO),
        )
    else:
        extra = "основное"
        template = texts.SCREEN_RULES_MAIN
        card = render_info_card(
            club,
            cache_tag="rules_main",
            badge="ПРАВИЛА КЛУБА",
            title="Правила посещения и брони",
            subtitle=f"г. {club.city}, {club.address} · работаем {club.hours}",
            highlights=[
                (
                    "Подтверждение",
                    "Заявка подтверждается администратором за 1–2 минуты.",
                ),
                (
                    "Ожидание 15 минут",
                    "При опоздании более 15 минут бронь может быть снята.",
                ),
                (
                    "Отмена и возврат",
                    "Не позднее чем за 1 час до начала; снять гостя можно на входе.",
                ),
                (
                    "Ночной пакет 18+",
                    f"Ночь ({texts.NIGHT_SLOT}) до 18 лет — только с согласия родителей.",
                ),
            ],
            accent=tone_rgba(Tone.INDIGO),
        )

    body = texts.render(
        template,
        club,
        night_slot=texts.NIGHT_SLOT,
        morning_slot=texts.MORNING_SLOT,
        day_slot=texts.DAY_SLOT,
    )
    return ScreenView(
        card=card,
        caption=_compose(Screen.RULES, club, body, extra=extra),
        reply_markup=rules_tabs_keyboard(tab),
    )


async def _render_faq_list(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    card = render_info_card(
        club,
        cache_tag="faq_list",
        badge="FAQ · ВОПРОСЫ И ОТВЕТЫ",
        title="Частые вопросы гостей",
        subtitle=f"Короткие и точные ответы по работе кибер-клуба {club.name}",
        highlights=[
            ("Ночной пакет", f"С {texts.NIGHT_SLOT}, оплачивается целиком за ночь."),
            ("Предоплата", "Нужна только на ночь в ПТ–ВС; в остальные дни — на месте."),
            (
                "Единый номер",
                f"{club.payment_system} {club.payment_card} ({club.payment_holder}).",
            ),
            ("FACEIT и залы", "FACEIT работает во всех 4 залах клуба."),
        ],
        accent=tone_rgba(Tone.AMBER),
    )
    body = "Выберите вопрос в списке кнопок ниже:"
    return ScreenView(
        card=card,
        caption=_compose(Screen.FAQ, club, body),
        reply_markup=faq_list_keyboard(),
    )


async def _render_faq_item(ctx: ViewContext, arg: str) -> ScreenView:
    item = texts.FAQ_BY_KEY.get(arg)
    if item is None:
        return await _render_faq_list(ctx, "")
    club = ctx.cfg.club
    answer_html = texts.faq_answer(item, club)
    item_tone = FAQ_TONES.get(item.key, Tone.SAPPHIRE)
    card = render_info_card(
        club,
        cache_tag=f"faq_{item.key}",
        badge="ОТВЕТ НА ВОПРОС",
        title=item.question,
        subtitle=f"Клуб {club.place} · {club.hours}",
        highlights=[
            ("Ответ клуба", _strip_html(answer_html)),
            ("Связь с админом", f"{club.admin_phone} ({club.admin_name})"),
        ],
        accent=tone_rgba(item_tone),
    )
    body = f"<b>{item.emoji} {escape_html(item.question)}</b>\n\n<blockquote>{answer_html}</blockquote>"
    return ScreenView(
        card=card,
        caption=_compose(Screen.FAQ_ITEM, club, body),
        reply_markup=faq_answer_keyboard(),
    )


async def _render_contacts(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    return ScreenView(
        card=render_contacts_card(club),
        caption=_compose(
            Screen.CONTACTS, club, texts.render(texts.SCREEN_CONTACTS_CARD_BODY, club)
        ),
        reply_markup=footer_rows(
            [
                btn(
                    "💳 Скопировать реквизиты",
                    copy_value=club.payment_card,
                    style=Surface.ACCENT,
                    tone=Tone.SAPPHIRE,
                ),
                btn(
                    "📞 Скопировать телефон",
                    copy_value=club.admin_phone,
                    style=Surface.ACCENT,
                    tone=Tone.CRYO,
                ),
            ],
            [(BTN_BOOK_HERO, nav(Screen.BOOK_TARIFF), Surface.HERO, Tone.MINT)],
            [("🖥 Залы и цены", nav(Screen.PRICES), Surface.GLASS, Tone.AMBER)],
        ),
    )


async def _render_bookings(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    bookings = await ctx.db.list_user_bookings(ctx.user_id, limit=6)
    if not bookings:
        body = texts.SCREEN_BOOKINGS_EMPTY
    else:
        body = "\n\n".join(
            texts.SCREEN_BOOKINGS_ITEM.format(
                number=booking.id,
                tariff=escape_html(booking.tariff),
                time_slot=escape_html(booking.time_slot),
                created=format_datetime(
                    booking.created_at, ctx.cfg.timezone, "%d.%m %H:%M"
                ),
                status=status_tag(booking.status),
            )
            for booking in bookings
        )
    return ScreenView(
        card=render_bookings_list_card(club, bookings, ctx.cfg.timezone),
        caption=_compose(Screen.BOOKINGS, club, body),
        reply_markup=footer_rows(
            [(BTN_BOOK_HERO, nav(Screen.BOOK_TARIFF), Surface.HERO, Tone.MINT)],
            [(BTN_REFRESH, nav(Screen.BOOKINGS), Surface.GLASS, Tone.SLATE)],
        ),
    )


# --------------------------------------------------------------------------- #
# Мастер бронирования (4 шага)
# --------------------------------------------------------------------------- #
async def _render_book_tariff(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    today = datetime.now(ctx.cfg.timezone).date()
    body = texts.render(
        texts.SCREEN_BOOK_TARIFF_BODY,
        club,
        tariffs_overview=texts.build_tariffs_overview(),
    )
    rows = _pairs(
        [
            (
                f"{tariff.emoji} {tariff.title} · {tariff.hour1} ₽/ч",
                f"{CB_TARIFF_PREFIX}{tariff.key}",
                Surface.GLASS,
                TARIFF_TONES.get(tariff.key, Tone.MINT),
            )
            for tariff in texts.TARIFFS
        ]
    )
    rows.append(
        [
            (BTN_BACK, CB_BOOKING_BACK, Surface.GLASS, Tone.SLATE),
            (BTN_CANCEL, CB_BOOKING_CANCEL, Surface.DANGER, Tone.RUBY),
        ]
    )
    return ScreenView(
        card=render_wizard_card(
            club,
            step=1,
            step_label="Выбор зала",
            is_weekend_day=texts.is_weekend(today),
        ),
        caption=_compose(Screen.BOOK_TARIFF, club, body),
        reply_markup=keyboard(*rows),
    )


async def _render_book_time(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    data = await _fsm(ctx)
    tariff = texts.get_tariff(str(data.get(DATA_TARIFF_KEY) or ""))
    if tariff is None:
        return await _render_book_tariff(ctx, "")

    moment = datetime.now(ctx.cfg.timezone)
    body = texts.render(
        texts.SCREEN_BOOK_TIME_BODY,
        club,
        tariff=escape_html(tariff.title),
        now_time=now_text(ctx.cfg.timezone),
        now_date=today_date_text(ctx.cfg.timezone),
    )
    rows: list[Sequence[ButtonSpec]] = []
    rest_choices: list[ButtonSpec] = []
    for key, label in texts.TIME_CHOICES:
        tone = TIME_TONES.get(key, Tone.SAPPHIRE)
        if key == texts.TIME_NOW_KEY:
            rows.append([(label, f"{CB_TIME_PREFIX}{key}", Surface.HERO, tone)])
        else:
            rest_choices.append((label, f"{CB_TIME_PREFIX}{key}", Surface.GLASS, tone))
    rows.extend(_pairs(rest_choices))
    return ScreenView(
        card=render_wizard_card(
            club,
            step=2,
            step_label="Время сессии",
            tariff_key=tariff.key,
            is_weekend_day=texts.is_weekend(moment.date()),
        ),
        caption=_compose(Screen.BOOK_TIME, club, body, extra=moment.strftime("%H:%M")),
        reply_markup=_with_step_nav(*rows),
    )


async def _render_book_time_text(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    data = await _fsm(ctx)
    tariff = texts.get_tariff(str(data.get(DATA_TARIFF_KEY) or ""))
    body = texts.render(
        texts.SCREEN_BOOK_TIME_TEXT_BODY,
        club,
        tariff=escape_html(tariff.title) if tariff else "—",
    )
    return ScreenView(
        card=render_wizard_card(
            club,
            step=2,
            step_label="Своё время сессии",
            tariff_key=tariff.key if tariff else None,
        ),
        caption=_compose(Screen.BOOK_TIME_TEXT, club, body),
        reply_markup=_with_step_nav(),
    )


async def _render_book_name(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    data = await _fsm(ctx)
    tariff = texts.get_tariff(str(data.get(DATA_TARIFF_KEY) or ""))
    time_slot = str(data.get(DATA_TIME_SLOT) or "—")
    today = datetime.now(ctx.cfg.timezone).date()
    amt = texts.amount_due(
        tariff.key if tariff else None, str(data.get(DATA_TIME_KEY) or ""), today
    )
    amt_str = (
        f"{amt} ₽" if amt is not None else f"от {tariff.hour1} ₽/ч" if tariff else None
    )

    body = texts.render(
        texts.SCREEN_BOOK_NAME_BODY,
        club,
        tariff=escape_html(tariff.title) if tariff else "—",
        time_slot=escape_html(time_slot),
    )
    return ScreenView(
        card=render_wizard_card(
            club,
            step=3,
            step_label="Имя гостя",
            tariff_key=tariff.key if tariff else None,
            time_slot=time_slot,
            amount_display=amt_str,
        ),
        caption=_compose(Screen.BOOK_NAME, club, body),
        reply_markup=_with_step_nav(),
    )


async def _render_book_pay(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    data = await _fsm(ctx)
    tariff_key = str(data.get(DATA_TARIFF_KEY) or "")
    tariff = texts.get_tariff(tariff_key)
    time_key = str(data.get(DATA_TIME_KEY) or "")
    time_slot = str(data.get(DATA_TIME_SLOT) or "—")
    client_name = str(data.get(DATA_CLIENT_NAME) or "—")
    today = datetime.now(ctx.cfg.timezone).date()
    required = texts.prepayment_required(time_key, today)

    amt_val = texts.amount_due(tariff_key, time_key, today)
    amt_card = (
        f"{amt_val} ₽"
        if amt_val is not None
        else (f"{tariff.hour1} ₽/ч (по прайсу)" if tariff else "По прайсу")
    )

    body = texts.render(
        texts.SCREEN_BOOK_PAY_BODY,
        club,
        tariff=escape_html(tariff.title) if tariff else "—",
        time_slot=escape_html(time_slot),
        client_name=escape_html(client_name),
        amount=texts.amount_text(tariff_key, time_key, today),
        payment_rules=texts.render(
            texts.PAYMENT_RULES_PREPAID if required else texts.PAYMENT_RULES_ON_SPOT,
            club,
        ),
    )

    rows: list[Sequence[ButtonSpec]] = [
        [("📸 Оплатил — отправить чек", CB_PAY_CHECK, Surface.HERO, Tone.MINT)],
    ]
    if not required:
        rows.append(
            [("💵 Оплачу на месте", CB_PAY_SPOT, Surface.GLASS, Tone.JADE)]
        )
    rows.append(
        [
            btn(
                "💳 Скопировать реквизиты",
                copy_value=club.payment_card,
                style=Surface.GLASS,
                tone=Tone.SAPPHIRE,
            )
        ]
    )

    return ScreenView(
        card=render_wizard_card(
            club,
            step=4,
            step_label="Способ оплаты",
            tariff_key=tariff_key,
            time_slot=time_slot,
            client_name=client_name,
            amount_display=amt_card,
            prepay_required=required,
        ),
        caption=_compose(Screen.BOOK_PAY, club, body),
        reply_markup=_with_step_nav(*rows),
    )


async def _render_book_receipt(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    data = await _fsm(ctx)
    tariff_key = str(data.get(DATA_TARIFF_KEY) or "")
    tariff = texts.get_tariff(tariff_key)
    time_key = str(data.get(DATA_TIME_KEY) or "")
    today = datetime.now(ctx.cfg.timezone).date()
    amt_val = texts.amount_due(tariff_key, time_key, today)
    amt_card = (
        f"{amt_val} ₽"
        if amt_val is not None
        else (f"{tariff.hour1} ₽/ч" if tariff else "По прайсу")
    )

    body = texts.render(
        texts.SCREEN_BOOK_RECEIPT_BODY,
        club,
        amount=texts.amount_text(tariff_key, time_key, today),
    )
    return ScreenView(
        card=render_wizard_card(
            club,
            step=4,
            step_label="Ожидание чека",
            tariff_key=tariff_key,
            time_slot=str(data.get(DATA_TIME_SLOT) or "—"),
            client_name=str(data.get(DATA_CLIENT_NAME) or "—"),
            amount_display=amt_card,
            waiting_receipt=True,
        ),
        caption=_compose(Screen.BOOK_RECEIPT, club, body),
        reply_markup=_with_step_nav(
            [
                btn(
                    "💳 Скопировать реквизиты",
                    copy_value=club.payment_card,
                    style=Surface.ACCENT,
                    tone=Tone.SAPPHIRE,
                )
            ]
        ),
    )


async def _render_book_done(ctx: ViewContext, arg: str) -> ScreenView:
    try:
        booking_id = int(arg)
    except (TypeError, ValueError):
        return await _render_menu(ctx, "")

    booking = await ctx.db.get_booking(booking_id)
    if booking is None:
        return await _render_menu(ctx, "")

    club = ctx.cfg.club
    body = texts.render(
        texts.SCREEN_BOOK_DONE_BODY,
        club,
        booking_id=booking.id,
        tariff=escape_html(booking.tariff),
        time_slot=escape_html(booking.time_slot),
        client_name=escape_html(booking.client_name),
        payment_note=(
            texts.SCREEN_DONE_PAID_CHECK
            if booking.receipt_file_id
            else texts.SCREEN_DONE_PAID_SPOT
        ),
    )
    return ScreenView(
        card=render_ticket_card(club, booking),
        caption=_compose(Screen.BOOK_DONE, club, body),
        reply_markup=footer_rows(
            [("📋 Мои брони", nav(Screen.BOOKINGS), Surface.ACCENT, Tone.SAPPHIRE)]
        ),
    )


# --------------------------------------------------------------------------- #
# 152-ФЗ: согласие, политика ПДн и отзыв согласия
# --------------------------------------------------------------------------- #
async def _render_consent(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    card = render_info_card(
        club,
        cache_tag="consent_152fz",
        badge="152-ФЗ · ЗАЩИТА ДАННЫХ",
        title=f"Добро пожаловать в {club.name}",
        subtitle="Подтвердите согласие на обработку данных для бронирования мест",
        highlights=[
            ("Только для брони", "Имя, выбранный зал, время сессии и чек оплаты."),
            (
                "Хранение в РФ",
                "База клуба без рекламных рассылок и передачи третьим лицам.",
            ),
            ("Отзыв в 1 клик", "Раздел /privacy отключает обработку в любой момент."),
            ("Оператор", f"{club.name}, г. {club.city} · {club.admin_phone}"),
        ],
        accent=tone_rgba(Tone.INDIGO),
    )
    body = texts.render(texts.SCREEN_CONSENT_CARD_BODY, club)
    return ScreenView(
        card=card,
        caption=_compose(Screen.CONSENT, club, body),
        reply_markup=consent_keyboard(),
    )


async def _render_privacy(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    card = render_info_card(
        club,
        cache_tag="privacy_152fz",
        badge="152-ФЗ · ПЕРСОНАЛЬНЫЕ ДАННЫЕ",
        title="Обработка персональных данных",
        subtitle=f"Оператор: {club.name}, г. {club.city}, {club.address}",
        highlights=[
            ("Что хранится", "Telegram ID, имя, параметры брони и фото чека оплаты."),
            (
                "Безопасность",
                "Сервер в РФ, доступ есть только у администраторов смены.",
            ),
            ("Отзыв согласия", "Кнопка ниже мгновенно останавливает работу бота."),
            (
                "Удаление записи",
                f"По обращению к {club.admin_phone} в течение 10 раб. дней.",
            ),
        ],
        accent=tone_rgba(Tone.INDIGO),
    )
    body = texts.render(texts.SCREEN_PRIVACY_CARD_BODY, club)
    return ScreenView(
        card=card,
        caption=_compose(Screen.PRIVACY, club, body),
        reply_markup=footer_rows(
            [("🚫 Отозвать согласие", CB_CONSENT_REVOKE, Surface.DANGER, Tone.RUBY)]
        ),
    )


async def _render_privacy_confirm(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    card = render_info_card(
        club,
        cache_tag="privacy_confirm",
        badge="ПОДТВЕРЖДЕНИЕ · 152-ФЗ",
        title="Отозвать согласие на ПДн?",
        subtitle="После отзыва меню, прайс и бронирование в боте будут заблокированы",
        highlights=[
            (
                "Что произойдёт",
                "Обработка ваших данных прекратится сразу (ч. 2 ст. 9 152-ФЗ).",
            ),
            (
                "Как вернуть доступ",
                "В любой момент кнопкой «Я согласен» или командой /start.",
            ),
        ],
        accent=COL_RED,
    )
    return ScreenView(
        card=card,
        caption=_compose(Screen.PRIVACY_CONFIRM, club, texts.CONSENT_REVOKE_CONFIRM),
        reply_markup=revoke_confirm_keyboard(),
    )


async def _render_consent_revoked(ctx: ViewContext, arg: str) -> ScreenView:
    club = ctx.cfg.club
    revoked_at = arg or now_full_text(ctx.cfg.timezone)
    card = render_info_card(
        club,
        cache_tag="consent_revoked",
        badge="БОТ ОСТАНОВЛЕН · 152-ФЗ",
        title="Согласие отозвано",
        subtitle="Доступ к меню и бронированию закрыт до повторного согласия",
        highlights=[
            ("Статус", f"Согласие отозвано ({revoked_at}). Заявки не принимаются."),
            (
                "Полное удаление",
                f"Напишите администратору: {club.admin_phone} ({club.admin_name}).",
            ),
        ],
        accent=COL_RED,
    )
    body = texts.render(texts.CONSENT_REVOKED, club, revoked_at=escape_html(revoked_at))
    return ScreenView(
        card=card,
        caption=_compose(Screen.CONSENT_REVOKED, club, body),
        reply_markup=consent_keyboard(),
    )


# --------------------------------------------------------------------------- #
# Админ-панель
# --------------------------------------------------------------------------- #
async def _render_admin(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    stats = await ctx.db.get_stats()
    now_str = now_text(ctx.cfg.timezone)
    body = texts.render(
        texts.SCREEN_ADMIN_BODY,
        club,
        now=now_str,
        **stats.as_dict(),
    )
    pending_label = (
        f"📋 Активные брони ({stats.pending})" if stats.pending else "📋 Активные брони"
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
            ("👥 Администраторы", nav(Screen.ADMIN_ADMINS), Surface.GLASS, Tone.AMETHYST),
            (BTN_REFRESH, nav(Screen.ADMIN), Surface.GLASS, Tone.SLATE),
        ],
        [
            (BTN_BACK, nav(SCREEN_BACK), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ],
    ]
    return ScreenView(
        card=render_admin_dashboard_card(club, stats, now_str),
        caption=_compose(Screen.ADMIN, club, body),
        reply_markup=keyboard(*rows),
    )


async def _render_admin_pending(ctx: ViewContext, arg: str) -> ScreenView:
    club = ctx.cfg.club
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
                client_link=user.link
                if user is not None
                else f"ID <code>{booking.user_id}</code>",
                created=format_datetime(
                    booking.created_at, ctx.cfg.timezone, "%d.%m %H:%M"
                ),
                amount=_amount_text(booking.tariff, booking.time_slot, today),
            )
            for booking, user in entries
        )

    rows: list[Sequence[ButtonSpec]] = [
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
        for booking, _user in entries
    ]
    if pages > 1:
        rows.append(
            [
                ("◀", nav(Screen.ADMIN_PENDING, max(0, page - 1)), Surface.GLASS, Tone.SLATE),
                (f"{page + 1}/{pages}", nav(Screen.ADMIN_PENDING, page), Surface.GLASS, Tone.SLATE),
                ("▶", nav(Screen.ADMIN_PENDING, min(pages - 1, page + 1)), Surface.GLASS, Tone.SLATE),
            ]
        )
    rows.append([(BTN_REFRESH, nav(Screen.ADMIN_PENDING, page), Surface.GLASS, Tone.SLATE)])
    rows.append(
        [
            (BTN_BACK, nav(SCREEN_BACK), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    extra = f"стр. {page + 1}/{pages}" if pages > 1 else ""
    return ScreenView(
        card=render_admin_pending_card(
            club,
            entries,
            total_pending=total,
            page=page,
            pages=pages,
            tz=ctx.cfg.timezone,
        ),
        caption=_compose(Screen.ADMIN_PENDING, club, body, extra=extra),
        reply_markup=keyboard(*rows),
    )


async def _render_admin_admins(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    admins = await ctx.db.list_admins()
    viewer_is_owner = await _is_owner(ctx)
    total, active = await ctx.db.count_admins()

    blocks = [
        texts.render(
            texts.SCREEN_ADMIN_ADMINS_BODY,
            club,
            admins_total=total,
            admins_active=active,
        )
    ]
    if not admins:
        blocks.append(texts.ADMINS_EMPTY.strip())
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
        label = await _admin_short_label(ctx, admin.user_id)
        mark = "👑" if admin.is_owner else "👤"
        state = "🟢" if admin.is_active else "🔴"
        adm_tone = (
            Tone.RUBY
            if not admin.is_active
            else (Tone.AMBER if admin.is_owner else Tone.MINT)
        )
        rows.append(
            [
                (
                    f"{state} {mark} {label}",
                    nav(Screen.ADMIN_ADMIN, admin.user_id),
                    Surface.GLASS,
                    adm_tone,
                )
            ]
        )
    if viewer_is_owner:
        rows.append([("➕ Добавить", CB_ADMIN_ADD, Surface.HERO, Tone.MINT)])
    rows.append([(BTN_REFRESH, nav(Screen.ADMIN_ADMINS), Surface.GLASS, Tone.SLATE)])
    rows.append(
        [
            (BTN_BACK, nav(SCREEN_BACK), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    return ScreenView(
        card=render_admin_team_card(club, admins),
        caption=_compose(Screen.ADMIN_ADMINS, club, "\n\n".join(blocks)),
        reply_markup=keyboard(*rows),
    )


async def _render_admin_card(ctx: ViewContext, arg: str) -> ScreenView:
    try:
        target_id = int(arg)
    except (TypeError, ValueError):
        return await _render_admin_admins(ctx, "")

    admin = await ctx.db.get_admin(target_id)
    if admin is None:
        return await _render_admin_admins(ctx, "")

    club = ctx.cfg.club
    all_admins = await ctx.db.list_admins()
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
    if is_env_owner:
        body += f"\n\n{texts.ADMINS_OWNER_ENV_NOTE.strip()}"

    rows: list[Sequence[ButtonSpec]] = []
    if await _is_owner(ctx) and not is_env_owner:
        rows.append(
            [
                (
                    "⛔️ Отключить" if admin.is_active else "✅ Включить",
                    admin_toggle_callback(admin.user_id),
                    Surface.GLASS if admin.is_active else Surface.HERO,
                    Tone.AMBER if admin.is_active else Tone.MINT,
                )
            ]
        )
        role_row: list[ButtonSpec] = []
        if admin.role != AdminRole.OWNER:
            role_row.append(
                (
                    "👑 Сделать владельцем",
                    admin_promote_callback(admin.user_id),
                    Surface.ACCENT,
                    Tone.AMBER,
                )
            )
        if admin.role != AdminRole.OPERATOR:
            role_row.append(
                (
                    "👤 Сделать оператором",
                    admin_demote_callback(admin.user_id),
                    Surface.ACCENT,
                    Tone.SAPPHIRE,
                )
            )
        if role_row:
            rows.append(role_row)
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
        [(BTN_REFRESH, nav(Screen.ADMIN_ADMIN, admin.user_id), Surface.GLASS, Tone.SLATE)]
    )
    rows.append(
        [
            ("◀ К списку", nav(Screen.ADMIN_ADMINS), Surface.GLASS, Tone.SLATE),
            (BTN_HOME, nav(Screen.MENU), Surface.GLASS, Tone.SLATE),
        ]
    )
    return ScreenView(
        card=render_admin_team_card(club, all_admins, selected=admin),
        caption=_compose(Screen.ADMIN_ADMIN, club, body),
        reply_markup=keyboard(*rows),
    )


async def _render_admin_add(ctx: ViewContext, _arg: str) -> ScreenView:
    club = ctx.cfg.club
    all_admins = await ctx.db.list_admins()
    body = texts.render(texts.ADMIN_ADD_PROMPT, club)
    return ScreenView(
        card=render_admin_team_card(club, all_admins, adding=True),
        caption=_compose(Screen.ADMIN_ADD, club, body),
        reply_markup=keyboard([(BTN_CANCEL, CB_ADMIN_ADD_CANCEL, Surface.DANGER, Tone.RUBY)]),
    )


def _amount_text(tariff_title: str, time_slot: str, today: Any) -> str:
    amount = texts.amount_for_booking(tariff_title, time_slot, today)
    return f"{amount} ₽" if amount is not None else "по прайсу"


async def _admin_label(ctx: ViewContext, user_id: int) -> str:
    stored = await ctx.db.get_user(user_id)
    if stored is not None:
        return stored.link
    return await _label_from_telegram(ctx, user_id, short=False)


async def _admin_short_label(ctx: ViewContext, user_id: int) -> str:
    stored = await ctx.db.get_user(user_id)
    if stored is not None:
        return stored.mention
    return await _label_from_telegram(ctx, user_id, short=True)


async def _label_from_telegram(ctx: ViewContext, user_id: int, *, short: bool) -> str:
    try:
        chat = await ctx.bot.get_chat(user_id)
    except Exception:  # noqa: BLE001
        logger.debug("Не удалось получить профиль администратора %s", user_id)
        return f"id{user_id}" if short else f"пользователь (ID: <code>{user_id}</code>)"
    username = getattr(chat, "username", None)
    if username:
        if short:
            return f"@{username}"
        return f'<a href="https://t.me/{escape_html(username)}">@{escape_html(username)}</a>'
    name = getattr(chat, "full_name", None) or getattr(chat, "first_name", None)
    if name:
        return (
            str(name) if short else f"{escape_html(name)} (ID: <code>{user_id}</code>)"
        )
    return f"id{user_id}" if short else f"пользователь (ID: <code>{user_id}</code>)"


# --------------------------------------------------------------------------- #
# Диспетчер экранов
# --------------------------------------------------------------------------- #
Renderer = Callable[[ViewContext, str], Awaitable[ScreenView]]

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
    Screen.CONSENT: _render_consent,
    Screen.PRIVACY: _render_privacy,
    Screen.PRIVACY_CONFIRM: _render_privacy_confirm,
    Screen.CONSENT_REVOKED: _render_consent_revoked,
    Screen.ADMIN: _render_admin,
    Screen.ADMIN_PENDING: _render_admin_pending,
    Screen.ADMIN_ADMINS: _render_admin_admins,
    Screen.ADMIN_ADMIN: _render_admin_card,
    Screen.ADMIN_ADD: _render_admin_add,
}


async def render_screen(ctx: ViewContext, screen: str, arg: str = "") -> ScreenView:
    """Собрать графический экран :class:`ScreenView`. Неизвестный экран — меню."""
    renderer = _RENDERERS.get(screen)
    if renderer is None:
        logger.debug("Неизвестный экран %r — показываю меню", screen)
        return await _render_menu(ctx, "")
    return await renderer(ctx, arg)


__all__ = [
    "CAPTION_LIMIT",
    "MESSAGE_LIMIT",
    "Renderer",
    "ScreenView",
    "ViewContext",
    "render_screen",
]
