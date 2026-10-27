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
from bot.db.models import AdminRole
from bot.keyboards.inline import (
    CB_BOOKING_BACK,
    CB_BOOKING_CANCEL,
    CB_BRANCH_PREFIX,
    CB_CLIENT_CANCEL_PREFIX,
    CB_CONSENT_REVOKE,
    CB_EXTRA_PREFIX,
    CB_GUESTS_PREFIX,
    CB_PAY_CHECK,
    CB_PAY_QR,
    CB_PAY_SPOT,
    CB_TARIFF_PREFIX,
    CB_TIME_PREFIX,
    consent_keyboard,
    revoke_confirm_keyboard,
)
from bot.services.admins import AdminRegistry
from bot.services.club_config import calculate_quote, effective_club_info
from bot.states import (
    DATA_BOOKING_DATE,
    DATA_BRANCH_ID,
    DATA_CLIENT_NAME,
    DATA_EXTRAS,
    DATA_GUEST_COMMENT,
    DATA_GUESTS_COUNT,
    DATA_TARIFF_KEY,
    DATA_TIME_KEY,
    DATA_TIME_SLOT,
)
from bot.ui.admin_views import (
    render_admin_add,
    render_admin_admins,
    render_admin_approved,
    render_admin_booking_cancel,
    render_admin_booking_card,
    render_admin_booking_edit,
    render_admin_booking_reject,
    render_admin_branch_item,
    render_admin_branches,
    render_admin_card,
    render_admin_handover,
    render_admin_home,
    render_admin_manual,
    render_admin_notify,
    render_admin_pending,
    render_admin_rejected,
    render_admin_schedule,
    render_admin_settings,
    render_admin_tariff_item,
    render_admin_tariffs,
)
from bot.ui.cards import (
    RenderedCard,
    clean_card_text,
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
    BTN_REFRESH,
    BTN_STEP_BACK,
    ButtonSpec,
    Screen,
    btn,
    faq_answer_keyboard,
    faq_list_keyboard,
    footer_rows,
    keyboard,
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


async def _fsm(ctx: ViewContext) -> dict[str, Any]:
    if ctx.state is None:
        return {}
    return await ctx.state.get_data()


async def _book_entry_screen(ctx: ViewContext) -> str:
    """Если активных точек >= 2 — шаг 0 выбор точки, иначе сразу выбор зала."""
    if await ctx.db.count_active_branches() >= 2:
        return Screen.BOOK_BRANCH
    return Screen.BOOK_TARIFF


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
    club, settings = await effective_club_info(ctx.db, ctx.cfg)
    role_title = await _admin_role_title(ctx)
    is_admin = role_title is not None
    card = render_menu_card(club, is_admin=is_admin, role_title=role_title)

    body = texts.render(texts.SCREEN_MENU_BODY, club)
    welcome_custom = (settings.get("welcome_text") or "").strip()
    if welcome_custom:
        body = f"<blockquote>👋 {escape_html(welcome_custom)}</blockquote>\n\n{body}"
    if is_admin:
        body += f"\n🛡 Доступ администратора активен (<b>{escape_html(role_title)}</b>)."

    book_screen = await _book_entry_screen(ctx)
    rows: list[Sequence[ButtonSpec]] = [
        [(BTN_BOOK_HERO, nav(book_screen), Surface.HERO, Tone.MINT)],
        [
            ("🖥 Залы и цены", nav(Screen.PRICES), Surface.GLASS, Tone.AMBER),
            ("📋 Мои брони", nav(Screen.BOOKINGS), Surface.GLASS, Tone.SAPPHIRE),
        ],
        [
            ("📘 Правила", nav(Screen.RULES), Surface.GLASS, Tone.INDIGO),
            ("💡 Вопросы", nav(Screen.FAQ), Surface.GLASS, Tone.TANGERINE),
        ],
        [
            ("📍 Контакты", nav(Screen.CONTACTS), Surface.GLASS, Tone.CRYO),
            ("🔒 Данные", nav(Screen.PRIVACY), Surface.GLASS, Tone.SLATE),
        ],
    ]
    chat_url = (settings.get("chat_url") or "").strip()
    if chat_url.startswith(("http://", "https://")):
        rows.append(
            [
                btn(
                    "💬 Общий чат («вопросы сюда»)",
                    url=chat_url,
                    style=Surface.GLASS,
                    tone=Tone.CRYO,
                )
            ]
        )
    if is_admin:
        rows.append(
            [("🛠 Админ-панель", nav(Screen.ADMIN), Surface.ACCENT, Tone.AMETHYST)]
        )

    return ScreenView(
        card=card,
        caption=_compose(Screen.MENU, club, body),
        reply_markup=keyboard(*rows),
    )


async def _render_prices(ctx: ViewContext, _arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    tariffs = await ctx.db.list_tariffs(active_only=True)
    extras = await ctx.db.list_tariff_extras(active_only=True)
    book_screen = await _book_entry_screen(ctx)

    night_weekend = " · ".join(f"{t.title} {t.night_weekend} ₽" for t in tariffs)
    blocks = [
        texts.render(
            texts.SCREEN_PRICES_BODY,
            club,
            morning_slot=texts.MORNING_SLOT,
            day_slot=texts.DAY_SLOT,
            night_slot=texts.NIGHT_SLOT,
        ),
        texts.render(texts.SCREEN_PRICES_NOTE, club, night_weekend=night_weekend),
    ]
    if extras:
        ext_line = " · ".join(
            f"{e.emoji} {escape_html(e.title)} "
            + (f"+{e.price} ₽" if e.price else "0 ₽")
            for e in extras
        )
        blocks.append(f"✨ <b>Доп. опции к брони:</b> {ext_line}")

    return ScreenView(
        card=render_prices_card(club, tariffs=tariffs),
        caption=_compose(Screen.PRICES, club, "\n\n".join(blocks)),
        reply_markup=footer_rows(
            [(BTN_BOOK_HERO, nav(book_screen), Surface.HERO, Tone.MINT)],
            [
                ("💡 Частые вопросы", nav(Screen.FAQ), Surface.GLASS, Tone.AMBER),
                ("📘 Правила", nav(Screen.RULES), Surface.GLASS, Tone.INDIGO),
            ],
        ),
    )


async def _render_rules(ctx: ViewContext, arg: str) -> ScreenView:
    club, settings = await effective_club_info(ctx.db, ctx.cfg)
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
            cache_tag=f"rules_pay_{club.payment_card}",
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
            cache_tag=f"rules_main_{club.address}",
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
    if tab == texts.RULES_TAB_MAIN:
        penalty = (settings.get("booking_rules_noshow_penalty") or "").strip()
        lead_h = (settings.get("booking_rules_lead_hours") or "0").strip()
        if penalty:
            body += f"\n\n⚠️ <b>Правило неявки:</b> {escape_html(penalty)}"
        if lead_h and lead_h != "0":
            body += (
                f"\n⏱ <b>Минимальное время до визита:</b> за {escape_html(lead_h)} ч."
            )

    return ScreenView(
        card=card,
        caption=_compose(Screen.RULES, club, body, extra=extra),
        reply_markup=rules_tabs_keyboard(tab),
    )


async def _render_faq_list(ctx: ViewContext, _arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    card = render_info_card(
        club,
        cache_tag=f"faq_list_{club.payment_card}",
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
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    answer_html = texts.faq_answer(item, club)
    item_tone = FAQ_TONES.get(item.key, Tone.SAPPHIRE)
    card = render_info_card(
        club,
        cache_tag=f"faq_{item.key}_{club.payment_card}",
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
    club, settings = await effective_club_info(ctx.db, ctx.cfg)
    branches = await ctx.db.list_branches(active_only=True)
    book_screen = await _book_entry_screen(ctx)

    body = texts.render(texts.SCREEN_CONTACTS_CARD_BODY, club)
    if len(branches) >= 2:
        br_lines = [
            f"📍 <b>{escape_html(b.name)}</b>: {escape_html(b.full_address)} ({escape_html(b.hours)})"
            for b in branches
        ]
        body += "\n\n<b>Наши точки:</b>\n" + "\n".join(br_lines)

    extra_rows: list[Sequence[ButtonSpec]] = [
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
    ]
    chat_url = (settings.get("chat_url") or "").strip()
    if chat_url.startswith(("http://", "https://")):
        extra_rows.append(
            [
                btn(
                    "💬 Вопросы сюда (общий чат)",
                    url=chat_url,
                    style=Surface.GLASS,
                    tone=Tone.CRYO,
                )
            ]
        )
    extra_rows.extend(
        [
            [(BTN_BOOK_HERO, nav(book_screen), Surface.HERO, Tone.MINT)],
            [("🖥 Залы и цены", nav(Screen.PRICES), Surface.GLASS, Tone.AMBER)],
        ]
    )
    return ScreenView(
        card=render_contacts_card(club),
        caption=_compose(Screen.CONTACTS, club, body),
        reply_markup=footer_rows(*extra_rows),
    )


async def _render_bookings(ctx: ViewContext, _arg: str) -> ScreenView:
    club, settings = await effective_club_info(ctx.db, ctx.cfg)
    bookings = await ctx.db.list_user_bookings(ctx.user_id, limit=6)
    allow_cancel = settings.get("booking_rules_allow_cancel") != "0"
    book_screen = await _book_entry_screen(ctx)

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
            + (
                f"\n   💵 Сумма: <b>{booking.quoted_price} ₽</b>"
                if booking.quoted_price
                else ""
            )
            for booking in bookings
        )

    rows: list[Sequence[ButtonSpec]] = []
    if allow_cancel:
        cancellable = [b for b in bookings if b.is_pending or b.is_approved][:2]
        if cancellable:
            rows.append(
                [
                    (
                        f"🚫 Отменить №{b.id}",
                        f"{CB_CLIENT_CANCEL_PREFIX}{b.id}",
                        Surface.DANGER,
                        Tone.RUBY,
                    )
                    for b in cancellable
                ]
            )
    rows.extend(
        [
            [(BTN_BOOK_HERO, nav(book_screen), Surface.HERO, Tone.MINT)],
            [(BTN_REFRESH, nav(Screen.BOOKINGS), Surface.GLASS, Tone.SLATE)],
        ]
    )
    return ScreenView(
        card=render_bookings_list_card(club, bookings, ctx.cfg.timezone),
        caption=_compose(Screen.BOOKINGS, club, body),
        reply_markup=footer_rows(*rows),
    )


# --------------------------------------------------------------------------- #
# Мастер бронирования (шаг 0 при сети 2+ точек + шаги 1..4)
# --------------------------------------------------------------------------- #
async def _render_book_branch(ctx: ViewContext, _arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    branches = await ctx.db.list_branches(active_only=True)
    if len(branches) < 2:
        return await _render_book_tariff(ctx, "")

    lines = ["🏢 <b>Выберите филиал клуба</b>, в котором хотите забронировать место:\n"]
    for br in branches:
        lines.append(
            f"📍 <b>{escape_html(br.name)}</b> — {escape_html(br.full_address)}\n"
            f"   🕒 {escape_html(br.hours)} · 📞 {escape_html(br.phone or club.admin_phone)}"
        )

    rows: list[Sequence[ButtonSpec]] = [
        [
            (
                f"📍 {br.name} ({br.city})",
                f"{CB_BRANCH_PREFIX}{br.id}",
                Surface.GLASS,
                Tone.INDIGO,
            )
        ]
        for br in branches
    ]
    rows.append(
        [
            (BTN_BACK, CB_BOOKING_BACK, Surface.GLASS, Tone.SLATE),
            (BTN_CANCEL, CB_BOOKING_CANCEL, Surface.DANGER, Tone.RUBY),
        ]
    )
    card = render_info_card(
        club,
        cache_tag=f"book_branch_{len(branches)}",
        badge="ВЫБОР ФИЛИАЛА · СЕТЬ КЛУБОВ",
        title="В какую точку идём?",
        subtitle="У каждой точки свои администраторы и свободные игровые места",
        highlights=[
            (br.name, f"{br.full_address} · {br.hours}") for br in branches[:3]
        ],
        accent=tone_rgba(Tone.INDIGO),
    )
    return ScreenView(
        card=card,
        caption=_compose(Screen.BOOK_BRANCH, club, "\n".join(lines)),
        reply_markup=keyboard(*rows),
    )


async def _render_book_tariff(ctx: ViewContext, _arg: str) -> ScreenView:
    data = await _fsm(ctx)
    branch_id = int(data.get(DATA_BRANCH_ID) or 1)
    club, _ = await effective_club_info(ctx.db, ctx.cfg, branch_id=branch_id)
    tariffs = await ctx.db.list_tariffs(active_only=True)
    today = datetime.now(ctx.cfg.timezone).date()
    weekend = texts.is_weekend(today)

    overview = "\n".join(
        f"{t.emoji} <b>{escape_html(t.title)}</b> — "
        f"{t.hour1_weekend if weekend else t.hour1} ₽/ч · "
        f"3ч {t.hour3_weekend if weekend else t.hour3} ₽ · "
        f"5ч {t.hour5_weekend if weekend else t.hour5} ₽ · "
        f"ночь {t.night_weekend if weekend else t.night} ₽"
        for t in tariffs
    )
    body = texts.render(
        texts.SCREEN_BOOK_TARIFF_BODY,
        club,
        tariffs_overview=overview,
    )
    if await ctx.db.count_active_branches() >= 2:
        br = await ctx.db.get_branch(branch_id)
        if br is not None:
            body = f"🏢 Филиал: <b>{escape_html(br.name)}</b>\n\n{body}"

    rows = _pairs(
        [
            (
                f"{tariff.emoji} {tariff.title} · {tariff.hour1_weekend if weekend else tariff.hour1} ₽/ч",
                f"{CB_TARIFF_PREFIX}{tariff.key}",
                Surface.GLASS,
                TARIFF_TONES.get(tariff.key, Tone.MINT),
            )
            for tariff in tariffs
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
            is_weekend_day=weekend,
        ),
        caption=_compose(Screen.BOOK_TARIFF, club, body),
        reply_markup=keyboard(*rows),
    )


async def _render_book_time(ctx: ViewContext, _arg: str) -> ScreenView:
    data = await _fsm(ctx)
    branch_id = int(data.get(DATA_BRANCH_ID) or 1)
    club, _ = await effective_club_info(ctx.db, ctx.cfg, branch_id=branch_id)
    tariff_key = str(data.get(DATA_TARIFF_KEY) or "")
    tariff = await ctx.db.get_tariff_record(tariff_key) or texts.get_tariff(tariff_key)
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
    data = await _fsm(ctx)
    branch_id = int(data.get(DATA_BRANCH_ID) or 1)
    club, _ = await effective_club_info(ctx.db, ctx.cfg, branch_id=branch_id)
    tariff_key = str(data.get(DATA_TARIFF_KEY) or "")
    tariff = await ctx.db.get_tariff_record(tariff_key) or texts.get_tariff(tariff_key)
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
    data = await _fsm(ctx)
    branch_id = int(data.get(DATA_BRANCH_ID) or 1)
    club, _ = await effective_club_info(ctx.db, ctx.cfg, branch_id=branch_id)
    tariff_key = str(data.get(DATA_TARIFF_KEY) or "")
    tariff = await ctx.db.get_tariff_record(tariff_key) or texts.get_tariff(tariff_key)
    time_key = str(data.get(DATA_TIME_KEY) or "")
    time_slot = str(data.get(DATA_TIME_SLOT) or "—")
    guests_count = int(data.get(DATA_GUESTS_COUNT) or 1)
    selected_extras = list(data.get(DATA_EXTRAS) or [])

    today = datetime.now(ctx.cfg.timezone).date()
    target_date_str = str(data.get(DATA_BOOKING_DATE) or "")
    try:
        target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    except ValueError:
        target_date = today

    total_quote, breakdown = await calculate_quote(
        ctx.db,
        tariff_key_or_title=tariff_key,
        time_key_or_slot=time_slot
        if (not time_key or time_key == texts.TIME_OTHER_KEY)
        else time_key,
        day=target_date,
        guests_count=guests_count,
        extra_keys=selected_extras,
    )
    amt_str = (
        f"{total_quote} ₽"
        if total_quote is not None
        else (f"от {tariff.hour1} ₽/ч" if tariff else None)
    )

    body = (
        texts.render(
            texts.SCREEN_BOOK_NAME_BODY,
            club,
            tariff=escape_html(tariff.title) if tariff else "—",
            time_slot=escape_html(time_slot),
        )
        + f"\n\n👥 Гостей: <b>{guests_count}</b> · 💰 Расчёт: <b>{escape_html(breakdown)}</b>"
        + "\n💡 <i>Комментарий можно указать через <code>//</code>: например <code>Алексей // свой ПК, тихий угол</code></i>"
    )

    extras_catalog = await ctx.db.list_tariff_extras(active_only=True)
    rows: list[Sequence[ButtonSpec]] = [
        [
            (
                f"{'✅' if guests_count == n else '👤'} {n} чел.",
                f"{CB_GUESTS_PREFIX}{n}",
                Surface.GLASS,
                Tone.MINT if guests_count == n else Tone.SLATE,
            )
            for n in (1, 2, 3, 4)
        ]
    ]
    if extras_catalog:
        ext_buttons: list[ButtonSpec] = [
            (
                f"{'🟢' if e.key in selected_extras else '⚪️'} {e.emoji} {e.title}"
                + (f" (+{e.price}₽)" if e.price else ""),
                f"{CB_EXTRA_PREFIX}{e.key}",
                Surface.GLASS,
                Tone.MINT if e.key in selected_extras else Tone.CRYO,
            )
            for e in extras_catalog[:4]
        ]
        rows.extend(_pairs(ext_buttons, per_row=2))

    return ScreenView(
        card=render_wizard_card(
            club,
            step=3,
            step_label="Гости, опции и имя",
            tariff_key=tariff.key if tariff else None,
            time_slot=time_slot,
            amount_display=amt_str,
        ),
        caption=_compose(Screen.BOOK_NAME, club, body),
        reply_markup=_with_step_nav(*rows),
    )


async def _render_book_pay(ctx: ViewContext, _arg: str) -> ScreenView:
    data = await _fsm(ctx)
    branch_id = int(data.get(DATA_BRANCH_ID) or 1)
    club, settings = await effective_club_info(ctx.db, ctx.cfg, branch_id=branch_id)
    tariff_key = str(data.get(DATA_TARIFF_KEY) or "")
    tariff = await ctx.db.get_tariff_record(tariff_key) or texts.get_tariff(tariff_key)
    time_key = str(data.get(DATA_TIME_KEY) or "")
    time_slot = str(data.get(DATA_TIME_SLOT) or "—")
    client_name = str(data.get(DATA_CLIENT_NAME) or "—")
    guests_count = int(data.get(DATA_GUESTS_COUNT) or 1)
    selected_extras = list(data.get(DATA_EXTRAS) or [])
    guest_comment = str(data.get(DATA_GUEST_COMMENT) or "")

    today = datetime.now(ctx.cfg.timezone).date()
    target_date_str = str(data.get(DATA_BOOKING_DATE) or "")
    try:
        target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    except ValueError:
        target_date = today

    required = texts.prepayment_required(time_key, target_date)
    total_quote, breakdown = await calculate_quote(
        ctx.db,
        tariff_key_or_title=tariff_key,
        time_key_or_slot=time_slot
        if (not time_key or time_key == texts.TIME_OTHER_KEY)
        else time_key,
        day=target_date,
        guests_count=guests_count,
        extra_keys=selected_extras,
    )
    amt_card = (
        f"{total_quote} ₽"
        if total_quote is not None
        else (f"{tariff.hour1} ₽/ч (по прайсу)" if tariff else "По прайсу")
    )
    amount_html = (
        f"<b>{escape_html(breakdown)}</b>"
        if total_quote is not None
        else texts.amount_text(tariff_key, time_key, target_date)
    )

    body = texts.render(
        texts.SCREEN_BOOK_PAY_BODY,
        club,
        tariff=escape_html(tariff.title) if tariff else "—",
        time_slot=escape_html(time_slot),
        client_name=escape_html(client_name),
        amount=amount_html,
        payment_rules=texts.render(
            texts.PAYMENT_RULES_PREPAID if required else texts.PAYMENT_RULES_ON_SPOT,
            club,
        ),
    )
    if guests_count > 1 or guest_comment:
        extra_meta = [f"👥 Гостей: <b>{guests_count}</b>"]
        if guest_comment:
            extra_meta.append(f"💬 <i>{escape_html(guest_comment[:60])}</i>")
        body += "\n" + " · ".join(extra_meta)

    rows: list[Sequence[ButtonSpec]] = [
        [("📸 Оплатил — отправить чек", CB_PAY_CHECK, Surface.HERO, Tone.MINT)],
    ]
    if not required:
        rows.append([("💵 Оплачу на месте", CB_PAY_SPOT, Surface.GLASS, Tone.JADE)])
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
    if settings.get("payment_photo_file_id"):
        rows.append(
            [("🖼 Показать QR / реквизиты", CB_PAY_QR, Surface.GLASS, Tone.AMBER)]
        )

    return ScreenView(
        card=render_wizard_card(
            club,
            step=4,
            step_label="Итоговая цена и оплата",
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
    data = await _fsm(ctx)
    branch_id = int(data.get(DATA_BRANCH_ID) or 1)
    club, _ = await effective_club_info(ctx.db, ctx.cfg, branch_id=branch_id)
    tariff_key = str(data.get(DATA_TARIFF_KEY) or "")
    tariff = await ctx.db.get_tariff_record(tariff_key) or texts.get_tariff(tariff_key)
    time_key = str(data.get(DATA_TIME_KEY) or "")
    time_slot = str(data.get(DATA_TIME_SLOT) or "—")
    guests_count = int(data.get(DATA_GUESTS_COUNT) or 1)
    selected_extras = list(data.get(DATA_EXTRAS) or [])

    today = datetime.now(ctx.cfg.timezone).date()
    total_quote, breakdown = await calculate_quote(
        ctx.db,
        tariff_key_or_title=tariff_key,
        time_key_or_slot=time_slot
        if (not time_key or time_key == texts.TIME_OTHER_KEY)
        else time_key,
        day=today,
        guests_count=guests_count,
        extra_keys=selected_extras,
    )
    amt_card = (
        f"{total_quote} ₽"
        if total_quote is not None
        else (f"{tariff.hour1} ₽/ч" if tariff else "По прайсу")
    )

    body = (
        texts.render(
            texts.SCREEN_BOOK_RECEIPT_BODY,
            club,
            amount=f"<b>{escape_html(breakdown)}</b>"
            if total_quote is not None
            else texts.amount_text(tariff_key, time_key, today),
        )
        + "\n📄 Принимаются как <b>фото/скриншот</b>, так и <b>PDF-документ</b> чека."
    )
    return ScreenView(
        card=render_wizard_card(
            club,
            step=4,
            step_label="Ожидание чека",
            tariff_key=tariff_key,
            time_slot=time_slot,
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

    club, _ = await effective_club_info(ctx.db, ctx.cfg, branch_id=booking.branch_id)
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
    if booking.quoted_price is not None:
        body += f"\n💰 Сумма к оплате: <b>{booking.quoted_price} ₽</b> (гостей: {booking.guests_count})"
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
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    card = render_info_card(
        club,
        cache_tag=f"consent_152fz_{club.name}",
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
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
    card = render_info_card(
        club,
        cache_tag=f"privacy_152fz_{club.name}",
        badge="152-ФЗ · ПЕРСОНАЛЬНЫЕ ДАННЫХ",
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
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
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
        accent=tone_rgba(Tone.RUBY),
    )
    return ScreenView(
        card=card,
        caption=_compose(Screen.PRIVACY_CONFIRM, club, texts.CONSENT_REVOKE_CONFIRM),
        reply_markup=revoke_confirm_keyboard(),
    )


async def _render_consent_revoked(ctx: ViewContext, arg: str) -> ScreenView:
    club, _ = await effective_club_info(ctx.db, ctx.cfg)
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
        accent=tone_rgba(Tone.RUBY),
    )
    body = texts.render(texts.CONSENT_REVOKED, club, revoked_at=escape_html(revoked_at))
    return ScreenView(
        card=card,
        caption=_compose(Screen.CONSENT_REVOKED, club, body),
        reply_markup=consent_keyboard(),
    )


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
    Screen.BOOK_BRANCH: _render_book_branch,
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
    Screen.ADMIN: render_admin_home,
    Screen.ADMIN_PENDING: render_admin_pending,
    Screen.ADMIN_APPROVED: render_admin_approved,
    Screen.ADMIN_REJECTED: render_admin_rejected,
    Screen.ADMIN_BOOKING: render_admin_booking_card,
    Screen.ADMIN_BOOKING_REJECT: render_admin_booking_reject,
    Screen.ADMIN_BOOKING_CANCEL: render_admin_booking_cancel,
    Screen.ADMIN_BOOKING_EDIT: render_admin_booking_edit,
    Screen.ADMIN_MANUAL: render_admin_manual,
    Screen.ADMIN_SCHEDULE: render_admin_schedule,
    Screen.ADMIN_TARIFFS: render_admin_tariffs,
    Screen.ADMIN_TARIFF_ITEM: render_admin_tariff_item,
    Screen.ADMIN_BRANCHES: render_admin_branches,
    Screen.ADMIN_BRANCH_ITEM: render_admin_branch_item,
    Screen.ADMIN_ADMINS: render_admin_admins,
    Screen.ADMIN_ADMIN: render_admin_card,
    Screen.ADMIN_ADD: render_admin_add,
    Screen.ADMIN_HANDOVER: render_admin_handover,
    Screen.ADMIN_NOTIFY: render_admin_notify,
    Screen.ADMIN_SETTINGS: render_admin_settings,
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
