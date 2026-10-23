"""Обработчики клиентской части бота.

Порядок регистрации хендлеров в роутере важен:

1. ``/cancel`` и ``/start`` — работают в любом состоянии FSM и сбрасывают его;
2. шаги сценария бронирования (сначала inline-кнопки, затем текстовый ввод);
3. «протухшие» кнопки бронирования — если сессия уже завершилась;
4. кнопки главного меню;
5. запасной хендлер — на всё остальное.

Все сообщения уходят в личные чаты (фильтр :class:`IsPrivateChat`) и только тем
пользователям, кто дал согласие на обработку ПДн (:class:`ConsentMiddleware`).
"""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot, F, Router
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
    CB_BOOKING_CANCEL,
    CB_CONSENT_GIVE,
    CB_TARIFF_PREFIX,
    CB_TIME_PREFIX,
    booking_cancel_keyboard,
    consent_keyboard,
    parse_tariff_key,
    parse_time_key,
    tariffs_keyboard,
    time_slots_keyboard,
)
from bot.keyboards.reply import (
    MENU_BOOKING,
    MENU_CONTACTS,
    MENU_PRICE,
    MENU_RULES,
    main_menu,
)
from bot.middlewares.consent import ConsentMiddleware
from bot.services.admins import AdminRegistry
from bot.services.booking import (
    clean_client_name,
    clean_custom_time,
    is_menu_button_text,
    tariff_title,
    time_slot_text,
)
from bot.services.notifications import deliver_new_booking
from bot.states import (
    DATA_CLIENT_NAME,
    DATA_TARIFF_KEY,
    DATA_TIME_KEY,
    DATA_TIME_SLOT,
    BookingFSM,
)
from bot.utils.formatting import (
    escape_html,
    now_full_text,
    now_text,
    today_date_text,
)
from bot.utils.telegram import edit_or_send, safe_delete_message

logger = logging.getLogger(__name__)

router = Router(name="client")

# Бот работает в личных чатах: уведомления о решении по брони шлются по user_id.
router.message.filter(IsPrivateChat())
router.callback_query.filter(IsPrivateChat())

# Один экземпляр на оба типа событий — чтобы кэш согласия был общим.
_consent_gate = ConsentMiddleware()
router.message.outer_middleware(_consent_gate)
router.callback_query.outer_middleware(_consent_gate)


async def _admin_greeting(
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None,
    user_id: int,
) -> str | None:
    """Если пользователь — администратор клуба, вернуть приветствие с его ролью.

    Администраторов может быть несколько (``ADMIN_IDS`` в ``.env`` плюс
    добавленные через панель), поэтому проверяем не один ID, а реестр/базу.
    """
    if admins is not None:
        is_admin = await admins.is_admin(user_id)
    else:
        is_admin = cfg.is_env_admin(user_id) or await db.is_active_admin(user_id)
    if not is_admin:
        return None

    admin = await db.get_admin(user_id)
    role = admin.role_title if admin is not None else AdminRole.title(AdminRole.OPERATOR)
    return texts.ADMIN_START_INFO.format(role=role)


# --------------------------------------------------------------------------- #
# 1. Служебные команды
# --------------------------------------------------------------------------- #
@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Отменить незавершённое бронирование из любого шага."""
    current_state = await state.get_state()
    await state.clear()

    if current_state is None:
        await message.answer(texts.BOOKING_NOT_STARTED, reply_markup=main_menu())
        return
    await message.answer(texts.BOOKING_CANCELED, reply_markup=main_menu())


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

    await state.clear()
    user = await db.upsert_user(sender.id, sender.username, sender.first_name)

    if not user.consent_given:
        _consent_gate.forget(sender.id)
        await message.answer(texts.CONSENT_TEXT, reply_markup=consent_keyboard())
        logger.info("Пользователь %s: запрошено согласие на обработку ПДн", sender.id)
        return

    _consent_gate.mark_consented(sender.id)
    await message.answer(
        texts.GREETING.format(user_name=escape_html(user.display_name)),
        reply_markup=main_menu(),
    )
    admin_greeting = await _admin_greeting(db, cfg, admins, sender.id)
    if admin_greeting:
        await message.answer(admin_greeting)


@router.callback_query(F.data == CB_CONSENT_GIVE)
async def cb_consent_give(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Кнопка «✅ Я согласен»: фиксируем согласие и открываем меню."""
    sender = callback.from_user
    if sender is None:
        await callback.answer()
        return

    await state.clear()
    await db.upsert_user(sender.id, sender.username, sender.first_name)
    await db.set_consent(sender.id, granted=True)
    _consent_gate.mark_consented(sender.id)

    user = await db.get_user(sender.id)
    display_name = escape_html(user.display_name if user else (sender.first_name or "гость"))

    await callback.answer("Согласие зафиксировано ✅")

    chat_id = callback.message.chat.id
    accepted = texts.CONSENT_ACCEPTED.format(consent_time=now_full_text(cfg.timezone))
    await edit_or_send(bot, chat_id, callback.message.message_id, accepted)

    await bot.send_message(
        chat_id=chat_id,
        text=texts.GREETING.format(user_name=display_name),
        reply_markup=main_menu(),
    )
    admin_greeting = await _admin_greeting(db, cfg, admins, sender.id)
    if admin_greeting:
        await bot.send_message(chat_id=chat_id, text=admin_greeting)
    logger.info("Пользователь %s дал согласие на обработку ПДн", sender.id)


@router.message(Command("privacy", "pdn"))
async def cmd_privacy(message: Message) -> None:
    """Политика обработки персональных данных (152-ФЗ) — доступна и без согласия."""
    await message.answer(texts.PRIVACY_TEXT, reply_markup=main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Краткая справка по работе с ботом."""
    await message.answer(texts.HELP_TEXT, reply_markup=main_menu())


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
        await message.answer(texts.ADMIN_NOT_ACTIVE, reply_markup=main_menu())
        logger.info("Отключённый администратор %s запрашивал панель", sender.id)
        return
    await message.answer(texts.ADMIN_NO_ACCESS, reply_markup=main_menu())
    logger.info("Пользователь %s запрашивал админ-панель без прав", sender.id if sender else "?")


# --------------------------------------------------------------------------- #
# 2. Сценарий бронирования: шаг 1 — тариф
# --------------------------------------------------------------------------- #
@router.callback_query(StateFilter(BookingFSM.choosing_tariff), F.data.startswith(CB_TARIFF_PREFIX))
async def cb_choose_tariff(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    cfg: AppConfig,
) -> None:
    """Клиент выбрал тариф — переходим к выбору времени."""
    tariff_key = parse_tariff_key(callback.data)
    if tariff_key is None:
        await callback.answer("Неизвестный тариф — начните бронирование заново", show_alert=True)
        return

    await state.update_data({DATA_TARIFF_KEY: tariff_key})
    await state.set_state(BookingFSM.choosing_time)

    await edit_or_send(
        bot,
        callback.message.chat.id,
        callback.message.message_id,
        texts.CHOOSE_TIME.format(
            tariff=escape_html(texts.TARIFF_TITLE_BY_KEY[tariff_key]),
            now_time=now_text(cfg.timezone),
            now_date=today_date_text(cfg.timezone),
        ),
        reply_markup=time_slots_keyboard(),
    )
    await callback.answer()
    logger.debug("Бронирование: выбран тариф %s", tariff_key)


# --------------------------------------------------------------------------- #
# 3. Шаг 2 — время
# --------------------------------------------------------------------------- #
@router.callback_query(StateFilter(BookingFSM.choosing_time), F.data.startswith(CB_TIME_PREFIX))
async def cb_choose_time(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    cfg: AppConfig,
) -> None:
    """«Сейчас» / «На ночь» — сразу к имени, «Другое» — к вводу времени текстом."""
    time_key = parse_time_key(callback.data)
    if time_key is None:
        await callback.answer("Неизвестный вариант времени", show_alert=True)
        return

    if time_key == texts.TIME_OTHER_KEY:
        await state.update_data({DATA_TIME_KEY: time_key})
        await state.set_state(BookingFSM.entering_custom_time)
        await edit_or_send(
            bot,
            callback.message.chat.id,
            callback.message.message_id,
            texts.ENTER_CUSTOM_TIME,
            reply_markup=booking_cancel_keyboard(),
        )
        await callback.answer()
        return

    slot = time_slot_text(time_key, datetime.now(cfg.timezone))
    if slot is None:
        await callback.answer("Не удалось определить время — выберите вариант из списка", show_alert=True)
        return

    data = await state.get_data()
    tariff_key = data.get(DATA_TARIFF_KEY)
    await state.update_data({DATA_TIME_KEY: time_key, DATA_TIME_SLOT: slot})
    await state.set_state(BookingFSM.entering_name)

    await edit_or_send(
        bot,
        callback.message.chat.id,
        callback.message.message_id,
        texts.ENTER_NAME.format(
            tariff=escape_html(tariff_title(tariff_key) or "—"),
            time_slot=escape_html(slot),
        ),
        reply_markup=booking_cancel_keyboard(),
    )
    await callback.answer()
    logger.debug("Бронирование: выбрано время %s", slot)


@router.message(StateFilter(BookingFSM.entering_custom_time), F.text)
async def msg_enter_custom_time(message: Message, state: FSMContext) -> None:
    """Клиент ввёл своё время текстом (кнопка «✏️ Другое»)."""
    if is_menu_button_text(message.text):
        await message.answer(
            texts.BOOKING_IN_PROGRESS_HINT.format(current_step=texts.STEP_HINT_TIME),
            reply_markup=booking_cancel_keyboard(),
        )
        return

    slot = clean_custom_time(message.text)
    if slot is None:
        await message.answer(texts.INVALID_CUSTOM_TIME)
        return

    data = await state.get_data()
    await state.update_data({DATA_TIME_KEY: texts.TIME_OTHER_KEY, DATA_TIME_SLOT: slot})
    await state.set_state(BookingFSM.entering_name)
    await message.answer(
        texts.ENTER_NAME.format(
            tariff=escape_html(tariff_title(data.get(DATA_TARIFF_KEY)) or "—"),
            time_slot=escape_html(slot),
        ),
        reply_markup=booking_cancel_keyboard(),
    )


# --------------------------------------------------------------------------- #
# 4. Шаг 3 — имя
# --------------------------------------------------------------------------- #
@router.message(StateFilter(BookingFSM.entering_name), F.text)
async def msg_enter_name(message: Message, state: FSMContext) -> None:
    """Клиент указал имя для брони — показываем реквизиты и ждём чек."""
    if is_menu_button_text(message.text):
        await message.answer(
            texts.BOOKING_IN_PROGRESS_HINT.format(current_step=texts.STEP_HINT_NAME),
            reply_markup=booking_cancel_keyboard(),
        )
        return

    client_name = clean_client_name(message.text)
    if client_name is None:
        await message.answer(texts.INVALID_NAME)
        return

    data = await state.get_data()
    tariff_key = data.get(DATA_TARIFF_KEY)
    time_key = data.get(DATA_TIME_KEY)
    time_slot = data.get(DATA_TIME_SLOT)

    if not tariff_key or not time_slot:
        # FSM-хранилище в памяти: после перезапуска бота черновик заявки теряется.
        await state.clear()
        await message.answer(texts.BOOKING_SESSION_LOST, reply_markup=main_menu())
        return

    await state.update_data({DATA_CLIENT_NAME: client_name})
    await state.set_state(BookingFSM.waiting_receipt)

    await message.answer(
        texts.PAYMENT_DETAILS.format(
            tariff=escape_html(tariff_title(tariff_key) or "—"),
            time_slot=escape_html(time_slot),
            client_name=escape_html(client_name),
            price_hint=escape_html(texts.tariff_price_hint(tariff_key, time_key or "")),
            payment_system=texts.PAYMENT_SYSTEM,
            payment_card=texts.PAYMENT_CARD,
            payment_holder=texts.PAYMENT_HOLDER,
        ),
        reply_markup=booking_cancel_keyboard(),
    )


# --------------------------------------------------------------------------- #
# 5. Шаг 4 — фото чека
# --------------------------------------------------------------------------- #
@router.message(StateFilter(BookingFSM.waiting_receipt), F.photo)
async def msg_receive_receipt(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
) -> None:
    """Принять фото чека, сохранить заявку и уведомить администратора."""
    sender = message.from_user
    if sender is None or not message.photo:
        return

    data = await state.get_data()
    tariff_key = data.get(DATA_TARIFF_KEY)
    time_slot = data.get(DATA_TIME_SLOT)
    client_name = data.get(DATA_CLIENT_NAME)

    if not tariff_key or not time_slot or not client_name:
        await state.clear()
        await message.answer(texts.BOOKING_SESSION_LOST, reply_markup=main_menu())
        return

    # message.photo отсортирован по возрастанию размера — берём самый большой.
    photo = message.photo[-1]

    user = await db.upsert_user(sender.id, sender.username, sender.first_name)
    booking = await db.add_booking(
        user_id=user.user_id,
        tariff=tariff_title(tariff_key) or tariff_key,
        time_slot=time_slot,
        client_name=client_name,
        receipt_file_id=photo.file_id,
    )
    await state.clear()

    await message.answer(
        texts.RECEIPT_RECEIVED.format(
            booking_id=booking.id,
            tariff=escape_html(booking.tariff),
            time_slot=escape_html(booking.time_slot),
            client_name=escape_html(booking.client_name),
        ),
        reply_markup=main_menu(),
    )

    # Заявка уходит всем активным администраторам; копии запоминаем в базе,
    # чтобы после решения погасить кнопки у тех, кто не успел нажать.
    report = await deliver_new_booking(bot, cfg, db, booking, user)
    for admin_id, message_id in report.notifications:
        await db.add_notification(booking.id, admin_id, message_id)
    if not report.ok:
        await message.answer(texts.RECEIPT_ADMIN_UNAVAILABLE.format(booking_id=booking.id))

    logger.info(
        "Заявка №%s создана: user=%s tariff=%s time=%s, администраторам: %s",
        booking.id,
        user.user_id,
        booking.tariff,
        booking.time_slot,
        report.summary(),
    )


@router.message(StateFilter(BookingFSM.waiting_receipt))
async def msg_receipt_expected(message: Message) -> None:
    """Клиент прислал что-то вместо фото чека (текст, документ, стикер…)."""
    if is_menu_button_text(message.text):
        await message.answer(
            texts.BOOKING_IN_PROGRESS_HINT.format(current_step=texts.STEP_HINT_RECEIPT),
            reply_markup=booking_cancel_keyboard(),
        )
        return

    await message.answer(
        texts.RECEIPT_NOT_PHOTO.format(
            payment_system=texts.PAYMENT_SYSTEM,
            payment_card=texts.PAYMENT_CARD,
            payment_holder=texts.PAYMENT_HOLDER,
        ),
        reply_markup=booking_cancel_keyboard(),
    )


# --------------------------------------------------------------------------- #
# 6. Кнопка отмены и «протухшие» кнопки бронирования
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == CB_BOOKING_CANCEL)
async def cb_cancel_booking(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Кнопка «❌ Отменить бронирование» внутри сценария."""
    current_state = await state.get_state()
    await state.clear()

    chat_id = callback.message.chat.id
    # Убираем сообщение с кнопками, чтобы клиент не нажимал их повторно.
    await safe_delete_message(bot, chat_id, callback.message.message_id)

    text = texts.BOOKING_CANCELED if current_state else texts.BOOKING_NOT_STARTED
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=main_menu())
    await callback.answer()


@router.callback_query(F.data.startswith(BOOKING_CALLBACK_PREFIXES))
async def cb_booking_outdated(callback: CallbackQuery) -> None:
    """Кнопка от старого сообщения: сценарий уже завершён или отменён."""
    await callback.answer(texts.BOOKING_SESSION_OUTDATED, show_alert=True)


# --------------------------------------------------------------------------- #
# 7. Главное меню
# --------------------------------------------------------------------------- #
@router.message(F.text == MENU_PRICE)
async def msg_price_list(message: Message) -> None:
    """Прайс-лист: Standart, VIP, S VIP, Double и ночные пакеты."""
    await message.answer(texts.build_price_list(), reply_markup=main_menu())


@router.message(F.text == MENU_RULES)
async def msg_rules(message: Message) -> None:
    """Правила клуба и условия бронирования."""
    await message.answer(texts.build_rules_text(), reply_markup=main_menu())


@router.message(F.text == MENU_CONTACTS)
async def msg_contacts(message: Message) -> None:
    """Контакты администратора: текст + карточка контакта (тап — звонок)."""
    await message.answer(texts.build_contacts_text(), reply_markup=main_menu())
    await message.answer_contact(
        phone_number=texts.ADMIN_PHONE_RAW,
        first_name=texts.ADMIN_NAME,
    )


@router.message(F.text == MENU_BOOKING)
async def msg_start_booking(message: Message, state: FSMContext) -> None:
    """Старт сценария бронирования: шаг 1 — выбор тарифа."""
    await state.clear()
    await state.set_state(BookingFSM.choosing_tariff)
    await message.answer(
        texts.CHOOSE_TARIFF.format(tariffs_overview=texts.build_tariffs_overview()),
        reply_markup=tariffs_keyboard(),
    )


# --------------------------------------------------------------------------- #
# 8. Запасные хендлеры
# --------------------------------------------------------------------------- #
@router.message(F.photo)
async def msg_photo_without_booking(message: Message, state: FSMContext) -> None:
    """Фото не на шаге оплаты: второй снимок из альбома или фото во время ввода.

    Если сценарий ещё идёт — подсказываем текущий шаг, иначе объясняем, что чек
    сохранять некуда, и предлагаем начать бронирование.
    """
    current_state = await state.get_state()
    step_hints = {
        BookingFSM.choosing_tariff.state: texts.STEP_HINT_TARIFF,
        BookingFSM.choosing_time.state: texts.STEP_HINT_TIME_BUTTON,
        BookingFSM.entering_custom_time.state: texts.STEP_HINT_TIME,
        BookingFSM.entering_name.state: texts.STEP_HINT_NAME,
    }
    hint = step_hints.get(current_state)
    if hint:
        await message.answer(
            texts.BOOKING_IN_PROGRESS_HINT.format(current_step=hint),
            reply_markup=booking_cancel_keyboard(),
        )
        return

    await message.answer(texts.PHOTO_WITHOUT_BOOKING, reply_markup=main_menu())


@router.message()
async def msg_unknown(message: Message) -> None:
    """Всё, что бот не распознал: подсказываем пользоваться меню."""
    await message.answer(texts.UNKNOWN_MESSAGE, reply_markup=main_menu())


__all__ = ["router"]
