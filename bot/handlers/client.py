"""Обработчики клиентской части бота.

Интерфейс построен вокруг одного сообщения-экрана: бот не отвечает новыми
сообщениями, а перезаписывает текущий экран (см. :mod:`bot.ui.navigator`).
Команды — это «точки входа» (``/start``, ``/menu``, ``/admin``), вся остальная
работа делается inline-кнопками под сообщением.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import texts
from bot.config import AppConfig
from bot.db.database import Database
from bot.filters.access import IsPrivateChat
from bot.keyboards.inline import (
    BOOKING_CALLBACK_PREFIXES,
    CB_BOOKING_BACK,
    CB_BOOKING_CANCEL,
    CB_BRANCH_PREFIX,
    CB_CLIENT_CANCEL_PREFIX,
    CB_CONSENT_GIVE,
    CB_CONSENT_REVOKE,
    CB_CONSENT_REVOKE_NO,
    CB_CONSENT_REVOKE_YES,
    CB_EXTRA_PREFIX,
    CB_GUESTS_PREFIX,
    CB_PAY_PREFIX,
    CB_PAY_QR,
    CB_TARIFF_PREFIX,
    CB_TIME_PREFIX,
    PAY_WAY_CHECK,
    PAY_WAY_SPOT,
    parse_payment_way,
    parse_tariff_key,
    parse_time_key,
)
from bot.middlewares.consent import ConsentMiddleware
from bot.services.admins import AdminRegistry
from bot.services.booking import (
    clean_custom_time,
    parse_name_and_comment,
    tariff_title,
    time_slot_text,
)
from bot.services.club_config import calculate_quote, effective_club_info
from bot.services.notifications import (
    deliver_client_cancel_push,
    deliver_new_booking,
    sync_peer_admins,
)
from bot.services.schedule import (
    check_slot_availability,
    extract_slot_window,
)
from bot.states import (
    DATA_ADMIN_MANUAL,
    DATA_ADMIN_REJECT_REASON,
    DATA_BOOKING_DATE,
    DATA_BRANCH_ID,
    DATA_CLIENT_NAME,
    DATA_EXTRAS,
    DATA_GUEST_COMMENT,
    DATA_GUESTS_COUNT,
    DATA_SCREEN,
    DATA_SCREEN_MESSAGE,
    DATA_SCREEN_STACK,
    DATA_SLOT_END,
    DATA_SLOT_START,
    DATA_TARIFF_KEY,
    DATA_TIME_KEY,
    DATA_TIME_SLOT,
    BookingFSM,
)
from bot.ui.navigator import Navigator
from bot.ui.screens import ADMIN_ONLY_SCREENS, SCREEN_BACK, Screen, ScreenCB
from bot.utils.formatting import escape_html, now_full_text

logger = logging.getLogger(__name__)

router = Router(name="client")

router.message.filter(IsPrivateChat())
router.callback_query.filter(IsPrivateChat())

_consent_gate = ConsentMiddleware()
router.message.outer_middleware(_consent_gate)
router.callback_query.outer_middleware(_consent_gate)

#: Экраны, доступные только администраторам (экспортируется для тестов).
ADMIN_SCREENS: frozenset[str] = ADMIN_ONLY_SCREENS


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #
def _navigate(
    event: Message | CallbackQuery,
    *,
    db: Database,
    cfg: AppConfig,
    state: FSMContext,
    admins: AdminRegistry | None = None,
) -> Navigator:
    """Собрать навигатор под конкретное событие."""
    if isinstance(event, CallbackQuery):
        return Navigator.from_callback(
            event, db=db, cfg=cfg, state=state, admins=admins
        )
    return Navigator.from_message(event, db=db, cfg=cfg, state=state, admins=admins)


async def _keep_navigation(state: FSMContext) -> dict[str, Any]:
    """Сбросить мастер бронирования, сохранив экран и историю переходов."""
    data = await state.get_data()
    await state.clear()
    await state.update_data(
        **{
            DATA_SCREEN: data.get(DATA_SCREEN),
            DATA_SCREEN_STACK: data.get(DATA_SCREEN_STACK) or [],
            DATA_SCREEN_MESSAGE: data.get(DATA_SCREEN_MESSAGE),
        }
    )
    return data


async def _is_admin(
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None,
    user_id: int,
) -> bool:
    """Проверка прав администратора (реестр → база → .env при пустой таблице)."""
    if admins is not None:
        return await admins.is_admin(user_id)
    if await db.is_active_admin(user_id):
        return True
    total, _ = await db.count_admins()
    if total == 0:
        return cfg.is_env_admin(user_id)
    return False


async def _create_booking(
    navigator: Navigator,
    db: Database,
    *,
    user_id: int,
    username: str | None,
    first_name: str | None,
    tariff_key: str,
    time_slot: str,
    client_name: str,
    receipt_file_id: str | None,
    receipt_kind: str = "",
    logger_message: str,
) -> int | None:
    """Сохранить заявку со всеми деталями, уведомить админов и показать билет."""
    bot = navigator.bot
    cfg = navigator.cfg
    data = await navigator.state.get_data()

    branch_id = int(data.get(DATA_BRANCH_ID) or 1)
    guests_count = int(data.get(DATA_GUESTS_COUNT) or 1)
    extras = list(data.get(DATA_EXTRAS) or [])
    guest_comment = str(data.get(DATA_GUEST_COMMENT) or "")
    time_key = str(data.get(DATA_TIME_KEY) or "")
    booking_date = str(
        data.get(DATA_BOOKING_DATE) or datetime.now(cfg.timezone).date().isoformat()
    )
    slot_start = str(data.get(DATA_SLOT_START) or "")
    slot_end = str(data.get(DATA_SLOT_END) or "")

    try:
        target_date = datetime.strptime(booking_date, "%Y-%m-%d").date()
    except ValueError:
        target_date = datetime.now(cfg.timezone).date()

    quoted_price, _ = await calculate_quote(
        db,
        tariff_key_or_title=tariff_key,
        time_key_or_slot=time_slot
        if (not time_key or time_key == texts.TIME_OTHER_KEY)
        else time_key,
        day=target_date,
        guests_count=guests_count,
        extra_keys=extras,
    )

    tariff_rec = await db.get_tariff_record(tariff_key)
    resolved_title = (
        tariff_rec.title
        if tariff_rec is not None
        else (tariff_title(tariff_key) or tariff_key)
    )

    user = await db.upsert_user(user_id, username, first_name)
    booking = await db.add_booking(
        user_id=user.user_id,
        tariff=resolved_title,
        time_slot=time_slot,
        client_name=client_name,
        receipt_file_id=receipt_file_id,
        receipt_kind=receipt_kind or "photo",
        branch_id=branch_id,
        booking_date=booking_date,
        slot_start=slot_start or None,
        slot_end=slot_end or None,
        guests_count=guests_count,
        guest_comment=guest_comment or None,
        extras=extras,
        quoted_price=quoted_price,
    )

    report = await deliver_new_booking(bot, cfg, db, booking, user)
    for admin_id, message_id in report.notifications:
        await db.add_notification(booking.id, admin_id, message_id)

    await navigator.state.set_state(None)
    await navigator.show(Screen.BOOK_DONE, booking.id, push=False)

    if not report.ok and report.failed:
        club, _ = await effective_club_info(db, cfg, branch_id=branch_id)
        await navigator.notify(
            texts.render(texts.RECEIPT_ADMIN_UNAVAILABLE, club, booking_id=booking.id)
        )

    logger.info(
        "%s: заявка №%s, user=%s, branch=%s, tariff=%s, time=%s, guests=%s, price=%s, админам: %s",
        logger_message,
        booking.id,
        user.user_id,
        branch_id,
        booking.tariff,
        booking.time_slot,
        guests_count,
        quoted_price,
        report.summary(),
    )
    return booking.id


# --------------------------------------------------------------------------- #
# 1. Точки входа: /start (в т. ч. инвайт adm_<token>), /menu, /cancel
# --------------------------------------------------------------------------- #
@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
    command: CommandObject | None = None,
) -> None:
    """Точка входа: согласие на обработку ПДн, активация инвайта админа или главное меню."""
    sender = message.from_user
    if sender is None:
        return

    data = await _keep_navigation(state)
    old_message_id = data.get(DATA_SCREEN_MESSAGE)
    user = await db.upsert_user(sender.id, sender.username, sender.first_name)

    invite_note: str | None = None
    raw_args = (command.args if command is not None else "") or ""
    if not raw_args and message.text and " " in message.text:
        raw_args = message.text.split(None, 1)[1].strip()
    if raw_args.startswith("adm_"):
        token = raw_args[4:].strip()
        activated = await db.consume_admin_invite(token, user_id=sender.id)
        if activated is not None:
            if not activated.display_name and (sender.full_name or sender.first_name):
                await db.set_admin_display_name(
                    sender.id, sender.full_name or sender.first_name
                )
            if admins is not None:
                admins.remember(sender.id, role=activated.role)
            if not user.consent_given:
                await db.set_consent(sender.id, granted=True)
                user = await db.get_user(sender.id) or user
            invite_note = f"🎉 Приглашение активировано! Ваша роль: <b>{escape_html(activated.role_title)}</b>."
            logger.info(
                "Активирован инвайт администратора: user=%s role=%s",
                sender.id,
                activated.role,
            )
        else:
            invite_note = (
                "⚠️ Ссылка-приглашение недействительна или уже была использована."
            )

    if not user.consent_given:
        _consent_gate.forget(sender.id)
        navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
        await navigator.restart(Screen.CONSENT, old_message_id=old_message_id)
        logger.info("Пользователь %s: запрошено согласие на обработку ПДн", sender.id)
        return

    _consent_gate.mark_consented(sender.id)
    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    if invite_note:
        await state.update_data(
            **{DATA_SCREEN_MESSAGE: old_message_id, DATA_SCREEN_STACK: []}
        )
        await navigator.show(Screen.MENU, push=False, note=invite_note)
    else:
        await navigator.restart(Screen.MENU, old_message_id=old_message_id)
    logger.info("Пользователь %s открыл главное меню", sender.id)


@router.message(Command("menu"))
async def cmd_menu(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Команда «вернуться в меню»."""
    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    await navigator.reset(Screen.MENU)


@router.message(Command("cancel"))
async def cmd_cancel(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Отменить незавершённое бронирование из любого шага."""
    current_state = await state.get_state()
    had_booking = current_state is not None
    await _keep_navigation(state)

    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    note = texts.BOOKING_CANCELED if had_booking else texts.BOOKING_NOT_STARTED
    await navigator.show(Screen.MENU, push=False, note=note)


# --------------------------------------------------------------------------- #
# 2. Согласие на обработку ПДн (152-ФЗ)
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == CB_CONSENT_GIVE)
async def cb_consent_give(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Кнопка «✅ Я согласен»: фиксируем согласие и открываем главное меню."""
    sender = callback.from_user
    if sender is None:
        await callback.answer()
        return

    await _keep_navigation(state)
    await db.upsert_user(sender.id, sender.username, sender.first_name)
    await db.set_consent(sender.id, granted=True)
    _consent_gate.mark_consented(sender.id)

    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    await navigator.show(Screen.MENU, push=False)
    await navigator.answer(f"Согласие зафиксировано ✅ ({now_full_text(cfg.timezone)})")
    logger.info("Пользователь %s дал согласие на обработку ПДн", sender.id)


@router.callback_query(F.data == CB_CONSENT_REVOKE)
async def cb_consent_revoke(
    callback: CallbackQuery,
    state: FSMContext | None = None,
    db: Database | None = None,
    cfg: AppConfig | None = None,
    admins: AdminRegistry | None = None,
) -> None:
    """Кнопка «🚫 Отозвать согласие»: карточка подтверждения в том же сообщении."""
    if state is not None and db is not None and cfg is not None:
        navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
        await navigator.show(Screen.PRIVACY_CONFIRM, push=False)
        await navigator.answer()
        return

    from bot.config import ClubInfo
    from bot.keyboards.inline import revoke_confirm_keyboard
    from bot.ui.cards import COL_RED, render_info_card
    from bot.utils.telegram import edit_or_send, safe_edit_message_media

    card = render_info_card(
        ClubInfo(),
        cache_tag="privacy_confirm",
        badge="ПОДТВЕРЖДЕНИЕ · 152-ФЗ",
        title="Отозвать согласие на ПДн?",
        subtitle="После отзыва меню и бронирование будут заблокированы",
        highlights=[
            ("152-ФЗ", "Обработка данных прекратится сразу после подтверждения.")
        ],
        accent=COL_RED,
    )
    if not await safe_edit_message_media(
        callback.bot,
        callback.message.chat.id,
        callback.message.message_id,
        card,
        texts.CONSENT_REVOKE_CONFIRM,
        revoke_confirm_keyboard(),
    ):
        await edit_or_send(
            callback.bot,
            callback.message.chat.id,
            callback.message.message_id,
            texts.CONSENT_REVOKE_CONFIRM,
            revoke_confirm_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == CB_CONSENT_REVOKE_NO)
async def cb_consent_revoke_no(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Отзыв отменён — возвращаем экран политики."""
    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    await navigator.show(Screen.PRIVACY, push=False, note=texts.CONSENT_REVOKE_CANCELED)
    await navigator.answer("Отзыв согласия отменён")


@router.callback_query(F.data == CB_CONSENT_REVOKE_YES)
async def cb_consent_revoke_yes(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Отзыв согласия: обработка ПДн прекращается, бот перестаёт работать."""
    sender = callback.from_user
    if sender is None:
        await callback.answer()
        return

    mid = callback.message.message_id if callback.message is not None else None
    await state.clear()
    if mid is not None:
        await state.update_data(**{DATA_SCREEN_MESSAGE: mid})

    await db.set_consent(sender.id, granted=False)
    _consent_gate.forget(sender.id)

    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    await navigator.show(
        Screen.CONSENT_REVOKED, now_full_text(cfg.timezone), push=False
    )
    await callback.answer("Согласие отозвано", show_alert=True)
    logger.info("Пользователь %s отозвал согласие на обработку ПДн", sender.id)


# --------------------------------------------------------------------------- #
# 3. Команды-экраны
# --------------------------------------------------------------------------- #
@router.message(Command("privacy", "pdn"))
async def cmd_privacy(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Политика обработки персональных данных."""
    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    await navigator.show(Screen.PRIVACY)


@router.message(Command("help"))
async def cmd_help(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Краткая справка над главным меню."""
    club, _ = await effective_club_info(db, cfg)
    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    await navigator.show(
        Screen.MENU, push=False, note=texts.render(texts.HELP_TEXT, club)
    )


@router.message(Command("admin", "stats", "admins", "adminlist"))
async def cmd_admin_no_access(
    message: Message,
    db: Database,
    state: FSMContext | None = None,
    cfg: AppConfig | None = None,
    admins: AdminRegistry | None = None,
) -> None:
    """Команды админ-панели от пользователя без прав."""
    sender = message.from_user
    admin = await db.get_admin(sender.id) if sender is not None else None
    if admin is not None and not admin.is_active:
        note = texts.ADMIN_NOT_ACTIVE
    else:
        note = texts.ADMIN_NO_ACCESS

    if state is not None and cfg is not None:
        navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
        cur = await navigator.current()
        await navigator.show(
            cur.get("s") or Screen.MENU,
            cur.get("a") or "",
            push=False,
            note=note,
        )
        return

    await message.answer(note)


# --------------------------------------------------------------------------- #
# 4. Навигация по экранам (nav:<screen>:<arg>) + отмена брони самим гостем
# --------------------------------------------------------------------------- #
@router.callback_query(ScreenCB.filter())
async def cb_navigate(
    callback: CallbackQuery,
    callback_data: ScreenCB,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Переход между экранами: меню, цены, FAQ, контакты, админ-панель."""
    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    screen = callback_data.screen
    arg: str | int = callback_data.arg

    if screen == SCREEN_BACK:
        cur = await navigator.current()
        screen, arg = await navigator.resolve_parent(
            cur.get("s") or Screen.MENU,
            cur.get("a") or "",
        )

    if screen in ADMIN_SCREENS and not await _is_admin(
        db, cfg, admins, callback.from_user.id
    ):
        await navigator.alert(texts.ADMIN_NO_ACCESS_ALERT)
        await navigator.show(Screen.MENU, push=False)
        return

    current_data = await state.get_data()
    has_transient_admin_draft = bool(
        current_data.get(DATA_ADMIN_MANUAL)
        or current_data.get(DATA_ADMIN_REJECT_REASON)
    )

    if screen == Screen.BOOK_BRANCH:
        await _keep_navigation(state)
        if await db.count_active_branches() >= 2:
            await state.set_state(BookingFSM.choosing_branch)
        else:
            screen = Screen.BOOK_TARIFF
            await state.set_state(BookingFSM.choosing_tariff)
    elif screen == Screen.BOOK_TARIFF:
        await _keep_navigation(state)
        if await db.count_active_branches() >= 2:
            screen = Screen.BOOK_BRANCH
            await state.set_state(BookingFSM.choosing_branch)
        else:
            branches = await db.list_branches(active_only=True)
            default_br = branches[0].id if branches else 1
            await state.update_data({DATA_BRANCH_ID: default_br})
            await state.set_state(BookingFSM.choosing_tariff)
    elif (
        screen in (Screen.ADMIN_BOOKING_REJECT, Screen.ADMIN_BOOKING_CANCEL)
        or (await state.get_state() is not None)
        or (screen != Screen.ADMIN_MANUAL and has_transient_admin_draft)
    ):
        await _keep_navigation(state)

    note = texts.BOOKING_CANCELED if callback_data.arg == "cancel" else None
    await navigator.show(screen, arg, note=note)
    await navigator.answer()


@router.callback_query(F.data.startswith(CB_CLIENT_CANCEL_PREFIX))
async def cb_client_self_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Гость сам отменяет свою активную бронь из экрана «Мои брони»."""
    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    raw_id = (callback.data or "")[len(CB_CLIENT_CANCEL_PREFIX) :]
    try:
        booking_id = int(raw_id)
    except ValueError:
        await navigator.alert("Некорректный номер брони")
        return

    _, settings = await effective_club_info(db, cfg)
    if settings.get("booking_rules_allow_cancel") == "0":
        await navigator.alert(
            "Самостоятельная отмена отключена правилами клуба — напишите администратору."
        )
        return

    booking = await db.get_booking(booking_id)
    if booking is None or booking.user_id != callback.from_user.id:
        await navigator.alert("Бронь не найдена")
        return
    if not (booking.is_pending or booking.is_approved):
        await navigator.alert("Эта бронь уже закрыта")
        await navigator.show(Screen.BOOKINGS, push=False)
        return

    ok = await db.client_cancel_booking(booking_id, callback.from_user.id)
    if not ok:
        await navigator.alert("Не удалось отменить бронь — обновите список")
        return

    fresh = await db.get_booking(booking_id) or booking
    user = await db.get_user(callback.from_user.id)
    await sync_peer_admins(navigator.bot, db, cfg, fresh)
    await deliver_client_cancel_push(navigator.bot, cfg, db, fresh, user)

    await navigator.show(
        Screen.BOOKINGS,
        push=False,
        note=f"🚫 Бронь <b>№{booking_id}</b> отменена. Будем рады видеть вас в удобное время!",
    )
    await navigator.answer("Бронь отменена")


# --------------------------------------------------------------------------- #
# 5. Мастер бронирования: шаг 0 — точка сети (при >= 2 активных точках)
# --------------------------------------------------------------------------- #
@router.callback_query(
    StateFilter(BookingFSM.choosing_branch), F.data.startswith(CB_BRANCH_PREFIX)
)
async def cb_choose_branch(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Клиент выбрал точку сети — переходим к выбору зала."""
    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    raw = (callback.data or "")[len(CB_BRANCH_PREFIX) :]
    try:
        branch_id = int(raw)
    except ValueError:
        await navigator.alert("Некорректный филиал")
        return

    branch = await db.get_branch(branch_id)
    if branch is None or not branch.is_active:
        await navigator.alert("Эта точка сейчас недоступна — выберите другую")
        await navigator.show(Screen.BOOK_BRANCH, push=False)
        return

    await state.update_data({DATA_BRANCH_ID: branch.id})
    await state.set_state(BookingFSM.choosing_tariff)
    await navigator.show(Screen.BOOK_TARIFF, push=False)
    await navigator.answer(f"Выбрана точка: {branch.name}")


# --------------------------------------------------------------------------- #
# 6. Мастер бронирования: шаг 1 — зал
# --------------------------------------------------------------------------- #
@router.callback_query(
    StateFilter(BookingFSM.choosing_tariff), F.data.startswith(CB_TARIFF_PREFIX)
)
async def cb_choose_tariff(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Клиент выбрал зал — переходим к выбору времени."""
    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    tariff_key = parse_tariff_key(callback.data)
    if tariff_key is None:
        raw_key = (callback.data or "")[len(CB_TARIFF_PREFIX) :]
        rec = await db.get_tariff_record(raw_key)
        tariff_key = rec.key if rec is not None and rec.is_active else None
    if tariff_key is None:
        await navigator.alert("Неизвестный зал — начните бронирование заново")
        return

    await state.update_data({DATA_TARIFF_KEY: tariff_key})
    await state.set_state(BookingFSM.choosing_time)
    await navigator.show(Screen.BOOK_TIME, push=False)
    await navigator.answer()


# --------------------------------------------------------------------------- #
# 7. Шаг 2 — время (с проверкой рабочих часов, ёмкости и буфера расписания)
# --------------------------------------------------------------------------- #
async def _validate_and_store_slot(
    db: Database,
    cfg: AppConfig,
    state: FSMContext,
    *,
    time_key: str,
    slot_text: str,
) -> tuple[bool, str]:
    now = datetime.now(cfg.timezone)
    data = await state.get_data()
    branch_id = int(data.get(DATA_BRANCH_ID) or 1)
    guests = int(data.get(DATA_GUESTS_COUNT) or 1)
    iso_date, start_hhmm, end_hhmm = extract_slot_window(slot_text, now)

    avail = await check_slot_availability(
        db,
        iso_date=iso_date,
        start_hhmm=start_hhmm,
        end_hhmm=end_hhmm,
        branch_id=branch_id,
        guests_count=guests,
        now_dt=now,
        is_instant_now=(time_key == texts.TIME_NOW_KEY),
    )
    if not avail.available:
        msg = f"⛔️ {avail.reason}"
        if avail.alternatives:
            msg += (
                "\n💡 Ближайшие свободные окна: <b>"
                + ", ".join(avail.alternatives)
                + "</b>."
            )
        return False, msg

    await state.update_data(
        {
            DATA_TIME_KEY: time_key,
            DATA_TIME_SLOT: slot_text,
            DATA_BOOKING_DATE: iso_date,
            DATA_SLOT_START: start_hhmm or "",
            DATA_SLOT_END: end_hhmm or "",
        }
    )
    return True, ""


@router.callback_query(
    StateFilter(BookingFSM.choosing_time), F.data.startswith(CB_TIME_PREFIX)
)
async def cb_choose_time(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """«Сейчас» / «Утро» / «День» / «Ночь» — к имени и опциям, «Другое» — к вводу текста."""
    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    time_key = parse_time_key(callback.data)
    if time_key is None:
        await navigator.alert("Неизвестный вариант времени")
        return

    if time_key == texts.TIME_OTHER_KEY:
        await state.update_data({DATA_TIME_KEY: time_key})
        await state.set_state(BookingFSM.entering_custom_time)
        await navigator.show(Screen.BOOK_TIME_TEXT, push=False)
        await navigator.answer()
        return

    slot = time_slot_text(time_key, datetime.now(cfg.timezone))
    if slot is None:
        await navigator.alert(
            "Не удалось определить время — выберите вариант из списка"
        )
        return

    ok, err_note = await _validate_and_store_slot(
        db, cfg, state, time_key=time_key, slot_text=slot
    )
    if not ok:
        await navigator.show(Screen.BOOK_TIME, push=False, note=err_note)
        await navigator.answer("Слот занят или вне расписания")
        return

    await state.set_state(BookingFSM.entering_name)
    await navigator.show(Screen.BOOK_NAME, push=False)
    await navigator.answer()


@router.message(StateFilter(BookingFSM.entering_custom_time), F.text)
async def msg_enter_custom_time(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Клиент ввёл своё время текстом (кнопка «✏️ Другое время»)."""
    from bot.utils.telegram import safe_delete_message

    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    slot = clean_custom_time(message.text)
    deleted = await safe_delete_message(
        navigator.bot, navigator.user_id, message.message_id
    )
    navigator.on_input_deleted(deleted)
    if slot is None:
        await navigator.show(
            Screen.BOOK_TIME_TEXT, push=False, note=texts.INVALID_CUSTOM_TIME
        )
        return

    ok, err_note = await _validate_and_store_slot(
        db, cfg, state, time_key=texts.TIME_OTHER_KEY, slot_text=slot
    )
    if not ok:
        await navigator.show(Screen.BOOK_TIME_TEXT, push=False, note=err_note)
        return

    await state.set_state(BookingFSM.entering_name)
    await navigator.show(Screen.BOOK_NAME, push=False)


# --------------------------------------------------------------------------- #
# 8. Шаг 3 — количество гостей, доп. опции («свой ПК», «VIP-зал», «напиток») и имя
# --------------------------------------------------------------------------- #
@router.callback_query(
    StateFilter(BookingFSM.entering_name), F.data.startswith(CB_GUESTS_PREFIX)
)
async def cb_choose_guests(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Переключить число гостей на шаге 3 (пересчитывает цену и проверяет вместимость)."""
    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    raw = (callback.data or "")[len(CB_GUESTS_PREFIX) :]
    try:
        count = max(1, min(10, int(raw)))
    except ValueError:
        await navigator.answer()
        return

    data = await state.get_data()
    start_hhmm = str(data.get(DATA_SLOT_START) or "") or None
    end_hhmm = str(data.get(DATA_SLOT_END) or "") or None
    iso_date = str(
        data.get(DATA_BOOKING_DATE) or datetime.now(cfg.timezone).date().isoformat()
    )
    if start_hhmm and end_hhmm:
        now = datetime.now(cfg.timezone)
        avail = await check_slot_availability(
            db,
            iso_date=iso_date,
            start_hhmm=start_hhmm,
            end_hhmm=end_hhmm,
            branch_id=int(data.get(DATA_BRANCH_ID) or 1),
            guests_count=count,
            now_dt=now,
            is_instant_now=(data.get(DATA_TIME_KEY) == texts.TIME_NOW_KEY),
        )
        if not avail.available:
            await navigator.alert(avail.reason)
            return

    await state.update_data({DATA_GUESTS_COUNT: count})
    await navigator.show(Screen.BOOK_NAME, push=False)
    await navigator.answer(f"Гостей: {count}")


@router.callback_query(
    StateFilter(BookingFSM.entering_name), F.data.startswith(CB_EXTRA_PREFIX)
)
async def cb_toggle_extra(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Вкл/выкл дополнительной опции («свой ПК», «VIP-зал», «напиток») на шаге 3."""
    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    extra_key = (callback.data or "")[len(CB_EXTRA_PREFIX) :]
    data = await state.get_data()
    selected = list(data.get(DATA_EXTRAS) or [])
    if extra_key in selected:
        selected.remove(extra_key)
    else:
        selected.append(extra_key)
    await state.update_data({DATA_EXTRAS: selected})
    await navigator.show(Screen.BOOK_NAME, push=False)
    await navigator.answer("Опции обновлены")


@router.message(StateFilter(BookingFSM.entering_name), F.text)
async def msg_enter_name(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Клиент указал имя (и опц. комментарий через //) — переходим к оплате с итоговой ценой."""
    from bot.utils.telegram import safe_delete_message

    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)

    client_name, comment = parse_name_and_comment(message.text)
    deleted = await safe_delete_message(
        navigator.bot, navigator.user_id, message.message_id
    )
    navigator.on_input_deleted(deleted)
    if client_name is None:
        await navigator.show(Screen.BOOK_NAME, push=False, note=texts.INVALID_NAME)
        return

    data = await state.get_data()
    if not data.get(DATA_TARIFF_KEY) or not data.get(DATA_TIME_SLOT):
        await _keep_navigation(state)
        await navigator.show(Screen.MENU, push=False, note=texts.BOOKING_SESSION_LOST)
        return

    await state.update_data(
        {DATA_CLIENT_NAME: client_name, DATA_GUEST_COMMENT: comment}
    )
    await state.set_state(BookingFSM.choosing_payment)
    await navigator.show(Screen.BOOK_PAY, push=False)


# --------------------------------------------------------------------------- #
# 9. Шаг 4 — итоговая цена и способ оплаты (чек / на месте / показать QR)
# --------------------------------------------------------------------------- #
@router.callback_query(StateFilter(BookingFSM.choosing_payment), F.data == CB_PAY_QR)
async def cb_show_pay_qr(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
) -> None:
    """Отправить фото/QR реквизитов оплаты, если оно загружено в настройках клуба."""
    _, settings = await effective_club_info(db, cfg)
    photo_id = settings.get("payment_photo_file_id") or ""
    if not photo_id:
        await callback.answer("Фото реквизитов не задано", show_alert=True)
        return
    await callback.bot.send_photo(
        chat_id=callback.from_user.id,
        photo=photo_id,
        caption="💳 <b>Реквизиты для перевода</b>",
    )
    _ = state
    await callback.answer("Реквизиты отправлены")


@router.callback_query(
    StateFilter(BookingFSM.choosing_payment), F.data.startswith(CB_PAY_PREFIX)
)
async def cb_choose_payment(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Клиент выбрал, как платит: чеком сейчас или на месте."""
    sender = callback.from_user
    if sender is None:
        await callback.answer()
        return

    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    way = parse_payment_way(callback.data)
    if way is None:
        await navigator.alert("Неизвестный способ оплаты — выберите кнопкой")
        return

    data = await state.get_data()
    tariff_key = data.get(DATA_TARIFF_KEY)
    time_key = data.get(DATA_TIME_KEY)
    time_slot = data.get(DATA_TIME_SLOT)
    client_name = data.get(DATA_CLIENT_NAME)

    if not tariff_key or not time_slot or not client_name:
        await _keep_navigation(state)
        await navigator.alert(texts.BOOKING_SESSION_LOST)
        await navigator.show(Screen.MENU, push=False)
        return

    day = datetime.now(cfg.timezone).date()
    if way == PAY_WAY_SPOT and texts.prepayment_required(time_key, day):
        await navigator.alert(texts.PREPAY_REQUIRED_ALERT)
        return

    if way == PAY_WAY_CHECK:
        await state.set_state(BookingFSM.waiting_receipt)
        await navigator.show(Screen.BOOK_RECEIPT, push=False)
        await navigator.answer()
        return

    await _create_booking(
        navigator,
        db,
        user_id=sender.id,
        username=sender.username,
        first_name=sender.first_name,
        tariff_key=tariff_key,
        time_slot=time_slot,
        client_name=client_name,
        receipt_file_id=None,
        logger_message="Оплата на месте",
    )
    await navigator.answer()


# --------------------------------------------------------------------------- #
# 10. Шаг 4б — приём чека (фото ИЛИ PDF-документ)
# --------------------------------------------------------------------------- #
@router.message(StateFilter(BookingFSM.waiting_receipt), F.photo)
async def msg_receive_receipt(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Принять фото чека, сохранить заявку и уведомить администратора."""
    sender = message.from_user
    if sender is None or not message.photo:
        return

    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    data = await state.get_data()
    tariff_key = data.get(DATA_TARIFF_KEY)
    time_slot = data.get(DATA_TIME_SLOT)
    client_name = data.get(DATA_CLIENT_NAME)

    if not tariff_key or not time_slot or not client_name:
        await _keep_navigation(state)
        await navigator.show(Screen.MENU, push=False, note=texts.BOOKING_SESSION_LOST)
        return

    photo = message.photo[-1]
    await _create_booking(
        navigator,
        db,
        user_id=sender.id,
        username=sender.username,
        first_name=sender.first_name,
        tariff_key=tariff_key,
        time_slot=time_slot,
        client_name=client_name,
        receipt_file_id=photo.file_id,
        receipt_kind="photo",
        logger_message="Оплата чеком (фото)",
    )


@router.message(StateFilter(BookingFSM.waiting_receipt), F.document)
async def msg_receive_pdf_receipt(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Принять PDF-документ чека (из банковского приложения)."""
    from bot.utils.telegram import safe_delete_message

    sender = message.from_user
    doc = message.document
    if sender is None or doc is None:
        return

    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    fname = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    is_pdf = mime == "application/pdf" or fname.endswith(".pdf")
    if not is_pdf:
        deleted = await safe_delete_message(
            navigator.bot, navigator.user_id, message.message_id
        )
        navigator.on_input_deleted(deleted)
        club, _ = await effective_club_info(db, cfg)
        await navigator.show(
            Screen.BOOK_RECEIPT,
            push=False,
            note=texts.render(texts.RECEIPT_NOT_PHOTO, club)
            + "\n📄 Поддерживается также формат <b>PDF</b>.",
        )
        return

    data = await state.get_data()
    tariff_key = data.get(DATA_TARIFF_KEY)
    time_slot = data.get(DATA_TIME_SLOT)
    client_name = data.get(DATA_CLIENT_NAME)
    if not tariff_key or not time_slot or not client_name:
        await _keep_navigation(state)
        await navigator.show(Screen.MENU, push=False, note=texts.BOOKING_SESSION_LOST)
        return

    await _create_booking(
        navigator,
        db,
        user_id=sender.id,
        username=sender.username,
        first_name=sender.first_name,
        tariff_key=tariff_key,
        time_slot=time_slot,
        client_name=client_name,
        receipt_file_id=doc.file_id,
        receipt_kind="document",
        logger_message="Оплата чеком (PDF)",
    )


@router.message(StateFilter(BookingFSM.waiting_receipt))
async def msg_receipt_expected(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Клиент прислал что-то вместо фото или PDF чека."""
    from bot.utils.telegram import safe_delete_message

    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    deleted = await safe_delete_message(
        navigator.bot, navigator.user_id, message.message_id
    )
    navigator.on_input_deleted(deleted)
    club, _ = await effective_club_info(db, cfg)
    await navigator.show(
        Screen.BOOK_RECEIPT,
        push=False,
        note=texts.render(texts.RECEIPT_NOT_PHOTO, club),
    )


# --------------------------------------------------------------------------- #
# 11. «Шаг назад» и отмена внутри мастера бронирования
# --------------------------------------------------------------------------- #
_STEP_BACK: dict[str | None, tuple[str | None, str]] = {
    BookingFSM.choosing_branch.state: (None, Screen.MENU),
    BookingFSM.choosing_tariff.state: (None, Screen.MENU),
    BookingFSM.choosing_time.state: (
        BookingFSM.choosing_tariff.state,
        Screen.BOOK_TARIFF,
    ),
    BookingFSM.entering_custom_time.state: (
        BookingFSM.choosing_time.state,
        Screen.BOOK_TIME,
    ),
    BookingFSM.entering_name.state: (BookingFSM.choosing_time.state, Screen.BOOK_TIME),
    BookingFSM.choosing_payment.state: (
        BookingFSM.entering_name.state,
        Screen.BOOK_NAME,
    ),
    BookingFSM.waiting_receipt.state: (
        BookingFSM.choosing_payment.state,
        Screen.BOOK_PAY,
    ),
}


@router.callback_query(F.data == CB_BOOKING_BACK)
async def cb_booking_back(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Кнопка «‹ Шаг назад»: вернуться на предыдущий шаг мастера."""
    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    current = await state.get_state()
    if current is None:
        await navigator.alert(texts.BOOKING_SESSION_OUTDATED)
        await navigator.show(Screen.MENU, push=False)
        return

    if (
        current == BookingFSM.choosing_tariff.state
        and await db.count_active_branches() >= 2
    ):
        await state.set_state(BookingFSM.choosing_branch)
        await navigator.show(Screen.BOOK_BRANCH, push=False)
        await navigator.answer()
        return

    next_state, screen = _STEP_BACK.get(current, (None, Screen.MENU))
    if next_state is None:
        await _keep_navigation(state)
        await navigator.show(Screen.MENU, push=False)
    else:
        await state.set_state(next_state)
        await navigator.show(screen, push=False)
    await navigator.answer()


@router.callback_query(F.data == CB_BOOKING_CANCEL)
async def cb_cancel_booking(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Кнопка «✕ Отменить» внутри мастера бронирования."""
    current = await state.get_state()
    await _keep_navigation(state)
    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    note = texts.BOOKING_CANCELED if current is not None else texts.BOOKING_NOT_STARTED
    await navigator.show(Screen.MENU, push=False, note=note)
    await navigator.answer()


@router.callback_query(F.data.startswith(BOOKING_CALLBACK_PREFIXES))
async def cb_booking_outdated(callback: CallbackQuery) -> None:
    """Кнопка от старого сообщения: сценарий уже завершён или отменён."""
    await callback.answer(texts.BOOKING_SESSION_OUTDATED, show_alert=True)


# --------------------------------------------------------------------------- #
# 12. Запасные хендлеры
# --------------------------------------------------------------------------- #
@router.message(F.photo)
async def msg_photo_without_booking(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Фото не на шаге оплаты."""
    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    current = await state.get_state()
    hints = {
        BookingFSM.choosing_branch.state: "сначала выберите филиал кнопкой на экране",
        BookingFSM.choosing_tariff.state: texts.STEP_HINT_TARIFF,
        BookingFSM.choosing_time.state: texts.STEP_HINT_TIME_BUTTON,
        BookingFSM.entering_custom_time.state: texts.STEP_HINT_TIME,
        BookingFSM.entering_name.state: texts.STEP_HINT_NAME,
        BookingFSM.choosing_payment.state: texts.STEP_HINT_PAYMENT,
        BookingFSM.waiting_receipt.state: texts.STEP_HINT_RECEIPT,
    }
    hint = hints.get(current)
    if hint:
        current_entry = await navigator.current()
        await navigator.show(
            current_entry.get("s") or Screen.MENU,
            current_entry.get("a") or "",
            push=False,
            note=texts.BOOKING_IN_PROGRESS_HINT.format(current_step=hint),
        )
        return

    await navigator.show(Screen.MENU, push=False, note=texts.PHOTO_WITHOUT_BOOKING)


@router.message()
async def msg_unknown(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Всё, что бот не распознал: показываем меню с подсказкой."""
    from bot.keyboards.reply import (
        MENU_BOOKING,
        MENU_CONTACTS,
        MENU_FAQ,
        MENU_PRICE,
        MENU_RULES,
    )

    legacy = {
        MENU_PRICE: Screen.PRICES,
        MENU_RULES: Screen.RULES,
        MENU_FAQ: Screen.FAQ,
        MENU_CONTACTS: Screen.CONTACTS,
        MENU_BOOKING: Screen.BOOK_TARIFF,
    }.get(message.text or "")
    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    if legacy is not None:
        if legacy == Screen.BOOK_TARIFF:
            await _keep_navigation(state)
            if await db.count_active_branches() >= 2:
                await state.set_state(BookingFSM.choosing_branch)
                await navigator.show(Screen.BOOK_BRANCH)
                return
            await state.set_state(BookingFSM.choosing_tariff)
        await navigator.show(legacy)
        return
    await navigator.show(Screen.MENU, push=False, note=texts.UNKNOWN_MESSAGE)


__all__ = ["ADMIN_SCREENS", "router"]
