"""Обработчики клиентской части бота.

Интерфейс построен вокруг одного сообщения-экрана: бот не отвечает новыми
сообщениями, а перезаписывает текущий экран (см. :mod:`bot.ui.navigator`).
Команды — это «точки входа» (``/start``, ``/menu``, ``/admin``), вся остальная
работа делается inline-кнопками под сообщением.

Порядок регистрации хендлеров в роутере важен:

1. ``/start``, ``/cancel``, ``/menu`` — работают в любом состоянии FSM;
2. кнопки согласия на обработку ПДн (152-ФЗ);
3. навигация по экранам (``nav:*``) — меню, цены, FAQ, контакты, админ-панель;
4. шаги мастера бронирования (inline-кнопки, затем текстовый ввод);
5. «протухшие» кнопки бронирования — если сессия уже завершена;
6. запасной хендлер — на всё остальное.

Все сообщения уходят в личные чаты (фильтр :class:`IsPrivateChat`) и только тем
пользователям, кто дал согласие на обработку ПДн (:class:`ConsentMiddleware`).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import texts
from bot.config import AppConfig
from bot.db.database import Database
from bot.db.models import AdminRole
from bot.filters.access import IsPrivateChat
from bot.keyboards.inline import (
    BOOKING_CALLBACK_PREFIXES,
    CB_BOOKING_BACK,
    CB_BOOKING_CANCEL,
    CB_CONSENT_GIVE,
    CB_CONSENT_REVOKE,
    CB_CONSENT_REVOKE_NO,
    CB_CONSENT_REVOKE_YES,
    CB_PAY_PREFIX,
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
    clean_client_name,
    clean_custom_time,
    tariff_title,
    time_slot_text,
)
from bot.services.notifications import deliver_new_booking
from bot.states import (
    DATA_CLIENT_NAME,
    DATA_SCREEN,
    DATA_SCREEN_MESSAGE,
    DATA_SCREEN_STACK,
    DATA_TARIFF_KEY,
    DATA_TIME_KEY,
    DATA_TIME_SLOT,
    BookingFSM,
)
from bot.ui.navigator import Navigator
from bot.ui.screens import Screen, ScreenCB
from bot.utils.formatting import now_full_text

logger = logging.getLogger(__name__)

router = Router(name="client")

# Бот работает в личных чатах: уведомления о решении по брони шлются по user_id.
router.message.filter(IsPrivateChat())
router.callback_query.filter(IsPrivateChat())

# Один экземпляр на оба типа событий — чтобы кэш согласия был общим.
_consent_gate = ConsentMiddleware()
router.message.outer_middleware(_consent_gate)
router.callback_query.outer_middleware(_consent_gate)

#: Экраны, доступные только администраторам.
ADMIN_SCREENS: frozenset[str] = frozenset(
    {
        Screen.ADMIN,
        Screen.ADMIN_PENDING,
        Screen.ADMIN_ADMINS,
        Screen.ADMIN_ADMIN,
        Screen.ADMIN_ADD,
    }
)


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
        return Navigator.from_callback(event, db=db, cfg=cfg, state=state, admins=admins)
    return Navigator.from_message(event, db=db, cfg=cfg, state=state, admins=admins)


async def _keep_navigation(state: FSMContext) -> dict[str, Any]:
    """Сбросить мастер бронирования, сохранив экран и историю переходов.

    :meth:`FSMContext.clear` стирает все данные, а нам нужны три ключа
    навигации — иначе после отмены бот потеряет сообщение-экран и заведёт
    новое, засорив чат.
    """
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


async def _admin_greeting(
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None,
    user_id: int,
) -> str | None:
    """Приветствие с ролью — только для администраторов клуба."""
    if not await _is_admin(db, cfg, admins, user_id):
        return None
    admin = await db.get_admin(user_id)
    role = admin.role_title if admin is not None else AdminRole.title(AdminRole.OPERATOR)
    return texts.render(texts.ADMIN_START_INFO, cfg.club, role=role)


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
    logger_message: str,
) -> int | None:
    """Сохранить заявку, уведомить администраторов, показать экран готово.

    Возвращает номер заявки или ``None``, если черновик оказался потерян
    (например, бот перезапускался — тогда данные FSM недоступны).
    """
    from aiogram import Bot

    bot = navigator.bot
    cfg = navigator.cfg

    user = await db.upsert_user(user_id, username, first_name)
    booking = await db.add_booking(
        user_id=user.user_id,
        tariff=tariff_title(tariff_key) or tariff_key,
        time_slot=time_slot,
        client_name=client_name,
        receipt_file_id=receipt_file_id,
    )

    report = await deliver_new_booking(bot, cfg, db, booking, user)
    for admin_id, message_id in report.notifications:
        await db.add_notification(booking.id, admin_id, message_id)

    await navigator.state.set_state(None)
    await navigator.show(Screen.BOOK_DONE, booking.id, push=False)

    if not report.ok:
        await navigator.notify(
            texts.render(texts.RECEIPT_ADMIN_UNAVAILABLE, cfg.club, booking_id=booking.id)
        )

    logger.info(
        "%s: заявка №%s, user=%s, tariff=%s, time=%s, администраторам: %s",
        logger_message,
        booking.id,
        user.user_id,
        booking.tariff,
        booking.time_slot,
        report.summary(),
    )
    _ = Bot  # импорт оставлен для типизации bot
    return booking.id


# --------------------------------------------------------------------------- #
# 1. Точки входа: /start, /menu, /cancel
# --------------------------------------------------------------------------- #
@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Точка входа: согласие на обработку ПДн (152-ФЗ) или главное меню."""
    sender = message.from_user
    if sender is None:
        return

    data = await _keep_navigation(state)
    old_message_id = data.get(DATA_SCREEN_MESSAGE)
    user = await db.upsert_user(sender.id, sender.username, sender.first_name)

    if not user.consent_given:
        _consent_gate.forget(sender.id)
        navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
        await navigator.restart(Screen.CONSENT, old_message_id=old_message_id)
        logger.info("Пользователь %s: запрошено согласие на обработку ПДн", sender.id)
        return

    _consent_gate.mark_consented(sender.id)
    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
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
    """Команда «вернуться в меню» — без сброса уже начатого бронирования."""
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
        highlights=[("152-ФЗ", "Обработка данных прекратится сразу после подтверждения.")],
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
    await navigator.show(Screen.CONSENT_REVOKED, now_full_text(cfg.timezone), push=False)
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
    """Политика обработки персональных данных — доступна и без согласия."""
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
    """Краткая справка: показываем её над главным меню."""
    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    await navigator.show(Screen.MENU, push=False, note=texts.render(texts.HELP_TEXT, cfg.club))


@router.message(Command("admin", "stats", "admins", "adminlist"))
async def cmd_admin_no_access(message: Message, db: Database) -> None:
    """Команды админ-панели от пользователя без прав.

    Админ-роутер такие события не принимает (фильтр :class:`IsAdmin`), поэтому
    они доходят сюда. Молчать нельзя: человек должен понимать, почему панель не
    открылась — особенно если его учётную запись администратора отключили.
    """
    sender = message.from_user
    admin = await db.get_admin(sender.id) if sender is not None else None
    if admin is not None and not admin.is_active:
        await message.answer(texts.ADMIN_NOT_ACTIVE)
        logger.info("Отключённый администратор %s запрашивал панель", sender.id)
        return
    await message.answer(texts.ADMIN_NO_ACCESS)
    logger.info("Пользователь %s запрашивал админ-панель без прав", sender.id if sender else "?")


# --------------------------------------------------------------------------- #
# 4. Навигация по экранам (nav:<screen>:<arg>)
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
    """Переход между экранами: меню, цены, FAQ, контакты, админ-панель.

    Один хендлер на все переходы: какой экран показать, записано в
    ``callback_data``. Экраны админ-панели доступны только администраторам —
    проверяем это здесь, потому что кнопку видит только тот, кому она
    показана, но кнопка из старого сообщения могла дожить и у другого.
    """
    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    screen = callback_data.screen

    if screen in ADMIN_SCREENS and not await _is_admin(db, cfg, admins, callback.from_user.id):
        await navigator.alert(texts.ADMIN_NO_ACCESS_ALERT)
        await navigator.show(Screen.MENU, push=False)
        logger.info(
            "Пользователь %s пытался открыть экран %s без прав",
            callback.from_user.id,
            screen,
        )
        return

    # Выход из мастера бронирования: если пользователь ушёл в другой раздел,
    # черновик заявки больше не актуален. А вход в мастер — переводит в шаг 1.
    if screen == Screen.BOOK_TARIFF:
        await _keep_navigation(state)
        await state.set_state(BookingFSM.choosing_tariff)
    elif await state.get_state() is not None:
        await _keep_navigation(state)

    note = texts.BOOKING_CANCELED if callback_data.arg == "cancel" else None
    await navigator.show(screen, callback_data.arg, note=note)
    await navigator.answer()


# --------------------------------------------------------------------------- #
# 5. Мастер бронирования: шаг 1 — зал
# --------------------------------------------------------------------------- #
@router.callback_query(StateFilter(BookingFSM.choosing_tariff), F.data.startswith(CB_TARIFF_PREFIX))
async def cb_choose_tariff(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Клиент выбрал зал — переходим к выбору времени."""
    tariff_key = parse_tariff_key(callback.data)
    if tariff_key is None:
        navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
        await navigator.alert("Неизвестный зал — начните бронирование заново")
        return

    await state.update_data({DATA_TARIFF_KEY: tariff_key})
    await state.set_state(BookingFSM.choosing_time)

    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    await navigator.show(Screen.BOOK_TIME, push=False)
    await navigator.answer()
    logger.debug("Бронирование: выбран зал %s", tariff_key)


# --------------------------------------------------------------------------- #
# 6. Шаг 2 — время
# --------------------------------------------------------------------------- #
@router.callback_query(StateFilter(BookingFSM.choosing_time), F.data.startswith(CB_TIME_PREFIX))
async def cb_choose_time(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """«Сейчас» / «Утро» / «День» / «Ночь» — к имени, «Другое» — к вводу текста."""
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
        await navigator.alert("Не удалось определить время — выберите вариант из списка")
        return

    await state.update_data({DATA_TIME_KEY: time_key, DATA_TIME_SLOT: slot})
    await state.set_state(BookingFSM.entering_name)
    await navigator.show(Screen.BOOK_NAME, push=False)
    await navigator.answer()
    logger.debug("Бронирование: выбрано время %s", slot)


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
    await safe_delete_message(navigator.bot, navigator.user_id, message.message_id)
    if slot is None:
        await navigator.show(Screen.BOOK_TIME_TEXT, push=False, note=texts.INVALID_CUSTOM_TIME)
        return

    await state.update_data({DATA_TIME_KEY: texts.TIME_OTHER_KEY, DATA_TIME_SLOT: slot})
    await state.set_state(BookingFSM.entering_name)
    await navigator.show(Screen.BOOK_NAME, push=False)


# --------------------------------------------------------------------------- #
# 7. Шаг 3 — имя
# --------------------------------------------------------------------------- #
@router.message(StateFilter(BookingFSM.entering_name), F.text)
async def msg_enter_name(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Клиент указал имя для брони — переходим к выбору способа оплаты."""
    from bot.utils.telegram import safe_delete_message

    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)

    client_name = clean_client_name(message.text)
    await safe_delete_message(navigator.bot, navigator.user_id, message.message_id)
    if client_name is None:
        await navigator.show(Screen.BOOK_NAME, push=False, note=texts.INVALID_NAME)
        return

    data = await state.get_data()
    if not data.get(DATA_TARIFF_KEY) or not data.get(DATA_TIME_SLOT):
        # FSM-хранилище в памяти: после перезапуска бота черновик заявки теряется.
        await _keep_navigation(state)
        await navigator.show(Screen.MENU, push=False, note=texts.BOOKING_SESSION_LOST)
        return

    await state.update_data({DATA_CLIENT_NAME: client_name})
    await state.set_state(BookingFSM.choosing_payment)
    await navigator.show(Screen.BOOK_PAY, push=False)
    logger.debug("Бронирование: имя %s", client_name)


# --------------------------------------------------------------------------- #
# 8. Шаг 4 — способ оплаты: прислать чек или оплатить на месте
# --------------------------------------------------------------------------- #
@router.callback_query(StateFilter(BookingFSM.choosing_payment), F.data.startswith(CB_PAY_PREFIX))
async def cb_choose_payment(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Клиент выбрал, как платит: чеком сейчас или на месте, при входе в клуб."""
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
        logger.info("Бронирование: черновик заявки потерян (перезапуск бота)")
        return

    day = datetime.now(cfg.timezone).date()

    # Ночной пакет в ПТ–ВС — только по полной предоплате: кнопки «на месте»
    # нет, но проверяем и здесь (кнопку могли нажать из старого сообщения,
    # отправленного в будни).
    if way == PAY_WAY_SPOT and texts.prepayment_required(time_key, day):
        await navigator.alert(texts.PREPAY_REQUIRED_ALERT)
        return

    if way == PAY_WAY_CHECK:
        await state.set_state(BookingFSM.waiting_receipt)
        await navigator.show(Screen.BOOK_RECEIPT, push=False)
        await navigator.answer()
        return

    # Оплата на месте: заявка создаётся без чека, администратор видит пометку.
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
# 9. Шаг 4б — фото чека
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

    # message.photo отсортирован по возрастанию размера — берём самый большой.
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
        logger_message="Оплата чеком",
    )


@router.message(StateFilter(BookingFSM.waiting_receipt))
async def msg_receipt_expected(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Клиент прислал что-то вместо фото чека (текст, документ, стикер…)."""
    from bot.utils.telegram import safe_delete_message

    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    await safe_delete_message(navigator.bot, navigator.user_id, message.message_id)
    await navigator.show(
        Screen.BOOK_RECEIPT,
        push=False,
        note=texts.render(texts.RECEIPT_NOT_PHOTO, cfg.club),
    )


# --------------------------------------------------------------------------- #
# 10. «Шаг назад» и отмена внутри мастера бронирования
# --------------------------------------------------------------------------- #
#: Куда ведёт «Шаг назад» из каждого состояния мастера.
_STEP_BACK: dict[str | None, tuple[str | None, str]] = {
    BookingFSM.choosing_tariff.state: (None, Screen.MENU),
    BookingFSM.choosing_time.state: (BookingFSM.choosing_tariff.state, Screen.BOOK_TARIFF),
    BookingFSM.entering_custom_time.state: (BookingFSM.choosing_time.state, Screen.BOOK_TIME),
    BookingFSM.entering_name.state: (BookingFSM.choosing_time.state, Screen.BOOK_TIME),
    BookingFSM.choosing_payment.state: (BookingFSM.entering_name.state, Screen.BOOK_NAME),
    BookingFSM.waiting_receipt.state: (BookingFSM.choosing_payment.state, Screen.BOOK_PAY),
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

    next_state, screen = _STEP_BACK.get(current, (None, Screen.MENU))
    await state.set_state(next_state)
    if next_state is None:
        await navigator.back()
    else:
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
# 11. Запасные хендлеры
# --------------------------------------------------------------------------- #
@router.message(F.photo)
async def msg_photo_without_booking(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Фото не на шаге оплаты: второй снимок из альбома или фото во время ввода.

    Если сценарий ещё идёт — подсказываем текущий шаг, иначе объясняем, что чек
    сохранять некуда, и предлагаем начать бронирование.
    """
    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    current = await state.get_state()
    hints = {
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
            await state.set_state(BookingFSM.choosing_tariff)
        await navigator.show(legacy)
        return
    await navigator.show(Screen.MENU, push=False, note=texts.UNKNOWN_MESSAGE)


__all__ = ["router", "ADMIN_SCREENS"]
