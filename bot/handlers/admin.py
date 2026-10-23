"""Админ-панель: заявки, статистика и управление администраторами клуба.

Доступ к роутеру закрыт фильтром :class:`IsAdmin`: администратором считается
активная запись в таблице ``admins`` (список берётся из ``ADMIN_IDS`` в ``.env``
и расширяется через панель).

Что умеет панель:

* ``/admin`` — статистика: всего заявок, ожидают, подтверждено, отклонено,
  сколько пользователей дали согласие на ПДн и сколько администраторов активно;
* «📋 Активные брони» — список ``pending``-заявок со страницами и кнопками
  «✅ Подтвердить» / «❌ Отклонить» по каждой;
* кнопки под фото чека — решение по конкретной заявке: статус пишется в базу
  атомарно (только если заявка ещё ``pending``), клиенту уходит уведомление,
  сообщение администратора обновляется отметкой о решении, а копии остальных
  администраторов гаснут;
* ``/admins`` — список администраторов. Владелец (первый ID в ``ADMIN_IDS``)
  может добавлять администраторов, включать и отключать их, назначать и снимать
  роль владельца, удалять из списка. Обычный администратор видит список без кнопок.

Заявки приходят сразу всем активным администраторам — решение принимает тот,
кто нажал первым; у остальных кнопки заменяются отметкой «обработано …».
"""

from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot import texts
from bot.config import AppConfig
from bot.db.database import Database
from bot.db.models import Admin, AdminRole, BookingStatus
from bot.filters.access import IsAdmin, IsPrivateChat
from bot.keyboards.inline import (
    CB_ADMIN_ADD,
    CB_ADMIN_ADD_CANCEL,
    CB_ADMIN_ADMINS,
    CB_ADMIN_APPROVE_PREFIX,
    CB_ADMIN_DELETE_PREFIX,
    CB_ADMIN_DEMOTE_PREFIX,
    CB_ADMIN_LIST_PREFIX,
    CB_ADMIN_PROMOTE_PREFIX,
    CB_ADMIN_REJECT_PREFIX,
    CB_ADMIN_STATS,
    CB_ADMIN_TOGGLE_PREFIX,
    PENDING_PAGE_SIZE,
    admin_add_cancel_keyboard,
    admin_panel_keyboard,
    admins_list_keyboard,
    admins_nav_keyboard,
    parse_admin_decision,
    parse_admin_delete,
    parse_admin_list_page,
    parse_admin_role,
    parse_admin_toggle,
    pending_list_keyboard,
)
from bot.services.admins import AdminRegistry
from bot.services.notifications import (
    CALLBACK_ANSWER_LIMIT,
    CAPTION_LIMIT,
    MESSAGE_LIMIT,
    decision_note,
    deliver_decision,
    resolve_decided_by,
    sync_peer_admins,
)
from bot.states import AdminFSM
from bot.utils.formatting import (
    escape_html,
    format_datetime,
    now_full_text,
    truncate,
)
from bot.utils.telegram import edit_or_send, safe_edit_message_caption

logger = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(IsAdmin(), IsPrivateChat())
router.callback_query.filter(IsAdmin(), IsPrivateChat())

#: Числовой Telegram ID: от 3 цифр, без @username и лишних символов.
_ADMIN_ID_RE = re.compile(r"^\d{3,}$")


# --------------------------------------------------------------------------- #
# Вспомогательные функции отрисовки
# --------------------------------------------------------------------------- #
async def _stats_text(db: Database, cfg: AppConfig) -> str:
    """Текст панели со статистикой (используется и /admin, и кнопка обновления)."""
    stats = await db.get_stats()
    return texts.render(
        texts.ADMIN_PANEL_GREETING,
        cfg.club,
        stats=texts.STATS_TEMPLATE.format(now=now_full_text(cfg.timezone), **stats.as_dict()),
    )


async def _render_pending(
    db: Database,
    cfg: AppConfig,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Собрать текст и клавиатуру страницы списка активных (``pending``) заявок."""
    total = await db.count_bookings(BookingStatus.PENDING)
    if total == 0:
        return texts.PENDING_EMPTY, admin_panel_keyboard()

    pages = max(1, (total + PENDING_PAGE_SIZE - 1) // PENDING_PAGE_SIZE)
    page = min(max(page, 0), pages - 1)

    entries = await db.list_bookings_with_users(
        BookingStatus.PENDING,
        limit=PENDING_PAGE_SIZE,
        offset=page * PENDING_PAGE_SIZE,
        oldest_first=True,
    )

    blocks = [
        texts.PENDING_LIST_HEADER.format(count=total, page=page + 1, pages=pages),
    ]
    for booking, user in entries:
        user_link = user.link if user is not None else f"ID: <code>{booking.user_id}</code>"
        blocks.append(
            texts.PENDING_ITEM.format(
                booking_id=booking.id,
                tariff=escape_html(booking.tariff),
                time_slot=escape_html(booking.time_slot),
                client_name=escape_html(booking.client_name),
                user_link=user_link,
                created_at=format_datetime(booking.created_at, cfg.timezone),
            )
        )

    keyboard = pending_list_keyboard([booking for booking, _ in entries], page, pages)
    return "\n\n".join(blocks), keyboard


async def _admin_label(bot: Bot, db: Database, user_id: int) -> str:
    """Как показать администратора в списке: «@nickname», «Имя (ID: …)».

    Администратор мог быть добавлен по ID и ещё ни разу не писать боту — тогда
    в таблице ``users`` его нет, пробуем спросить профиль у Telegram.
    """
    stored = await db.get_user(user_id)
    if stored is not None:
        return stored.link
    try:
        chat = await bot.get_chat(user_id)
    except Exception:  # noqa: BLE001 — профиль недоступен, показываем ID
        logger.debug("Не удалось получить профиль администратора %s", user_id)
        return f"пользователь (ID: <code>{user_id}</code>)"
    username = getattr(chat, "username", None)
    if username:
        return f'<a href="https://t.me/{escape_html(username)}">@{escape_html(username)}</a>'
    name = getattr(chat, "full_name", None) or getattr(chat, "first_name", None)
    if name:
        return f"{escape_html(name)} (ID: <code>{user_id}</code>)"
    return f"пользователь (ID: <code>{user_id}</code>)"


async def _is_owner(
    db: Database,
    admins: AdminRegistry | None,
    user_id: int | None,
) -> bool:
    """Может ли этот администратор управлять списком администраторов."""
    if not user_id:
        return False
    if admins is not None:
        return await admins.is_owner(user_id)
    admin = await db.get_admin(user_id)
    return admin is not None and admin.is_owner


async def _render_admins(
    bot: Bot,
    db: Database,
    cfg: AppConfig,
    *,
    viewer_id: int | None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура раздела «👥 Администраторы»."""
    admins = await db.list_admins()
    viewer_is_owner = await _is_owner(db, None, viewer_id)

    if not admins:
        blocks = [texts.ADMINS_EMPTY]
    else:
        total, active = await db.count_admins()
        blocks = [texts.ADMINS_LIST_HEADER.format(total=total, active=active)]
        for index, admin in enumerate(admins, start=1):
            blocks.append(await _render_admin_item(bot, db, admin, index))
            if admin.user_id == cfg.admin_id:
                blocks.append(texts.ADMINS_OWNER_ENV_NOTE)

    if not viewer_is_owner:
        viewer = await db.get_admin(viewer_id) if viewer_id else None
        role = viewer.role_title if viewer is not None else AdminRole.title(AdminRole.OPERATOR)
        blocks.append(texts.ADMIN_MANAGE_FORBIDDEN.format(role=role))

    keyboard = admins_list_keyboard(
        admins,
        env_owner_id=cfg.admin_id,
        viewer_is_owner=viewer_is_owner,
    )
    return "\n\n".join(blocks), keyboard


async def _render_admin_item(bot: Bot, db: Database, admin: Admin, index: int) -> str:
    """Одна строка списка администраторов."""
    state = texts.ADMINS_STATE_ACTIVE if admin.is_active else texts.ADMINS_STATE_DISABLED
    label = await _admin_label(bot, db, admin.user_id)
    item = texts.ADMINS_ITEM.format(
        index=index,
        user_link=label,
        user_id=admin.user_id,
        role=admin.role_title,
        state=state,
        added="",
    )
    if admin.added_by:
        added_by = await _admin_label(bot, db, admin.added_by)
        item += texts.ADMINS_ITEM_ADDED.format(added_by=added_by)
    return item


async def _apply_admin_view(
    bot: Bot,
    db: Database,
    cfg: AppConfig,
    callback: CallbackQuery,
    note: str,
) -> None:
    """Обновить сообщение администратора после решения по заявке.

    * если это фото чека — дописываем отметку в подпись и убираем кнопки;
    * если это текстовый список заявок — перерисовываем первую страницу списка
      (обработанная заявка из него исчезает).
    """
    message = callback.message
    chat_id, message_id = message.chat.id, message.message_id

    if getattr(message, "photo", None):
        caption = getattr(message, "caption", "") or ""
        if note.strip() in caption:  # отметка уже есть — не дублируем
            return
        updated = truncate(caption + note, CAPTION_LIMIT)
        if not await safe_edit_message_caption(bot, chat_id, message_id, updated):
            await bot.send_message(chat_id=chat_id, text=truncate(note.strip(), MESSAGE_LIMIT))
        return

    text, keyboard = await _render_pending(db, cfg, 0)
    await edit_or_send(bot, chat_id, message_id, truncate(text, MESSAGE_LIMIT), keyboard)


async def _refresh_admins_view(
    bot: Bot,
    db: Database,
    cfg: AppConfig,
    callback: CallbackQuery,
    note: str | None = None,
) -> None:
    """Перерисовать список администраторов в том же сообщении и ответить на клик."""
    text, keyboard = await _render_admins(bot, db, cfg, viewer_id=callback.from_user.id)
    await edit_or_send(
        bot,
        callback.message.chat.id,
        callback.message.message_id,
        truncate(text, MESSAGE_LIMIT),
        keyboard,
    )
    if note:
        await callback.answer(truncate(note, CALLBACK_ANSWER_LIMIT))
    else:
        await callback.answer()


async def _deny_management(
    callback: CallbackQuery,
    db: Database,
    user_id: int | None,
) -> None:
    """Сообщить, что управление списком доступно только владельцу."""
    admin = await db.get_admin(user_id) if user_id else None
    role = admin.role_title if admin is not None else AdminRole.title(AdminRole.OPERATOR)
    await callback.answer(
        truncate(texts.ADMIN_MANAGE_FORBIDDEN.format(role=role), CALLBACK_ANSWER_LIMIT),
        show_alert=True,
    )


# --------------------------------------------------------------------------- #
# Команда /admin — статистика
# --------------------------------------------------------------------------- #
@router.message(Command("admin", "stats"))
async def cmd_admin(message: Message, db: Database, cfg: AppConfig) -> None:
    """Показать статистику заявок и кнопки панели."""
    await message.answer(await _stats_text(db, cfg), reply_markup=admin_panel_keyboard())
    logger.info(
        "Админ-панель: %s запросил статистику",
        message.from_user.id if message.from_user else "?",
    )


@router.callback_query(F.data == CB_ADMIN_STATS)
async def cb_refresh_stats(callback: CallbackQuery, db: Database, bot: Bot, cfg: AppConfig) -> None:
    """Кнопка «🔄 Обновить статистику»."""
    text = await _stats_text(db, cfg)
    await edit_or_send(
        bot,
        callback.message.chat.id,
        callback.message.message_id,
        truncate(text, MESSAGE_LIMIT),
        admin_panel_keyboard(),
    )
    await callback.answer("Статистика обновлена")


@router.callback_query(F.data.startswith(CB_ADMIN_LIST_PREFIX))
async def cb_pending_list(callback: CallbackQuery, db: Database, bot: Bot, cfg: AppConfig) -> None:
    """Кнопка «📋 Активные брони» и постраничная навигация по списку."""
    page = parse_admin_list_page(callback.data) or 0
    text, keyboard = await _render_pending(db, cfg, page)

    await edit_or_send(
        bot,
        callback.message.chat.id,
        callback.message.message_id,
        truncate(text, MESSAGE_LIMIT),
        keyboard,
    )
    await callback.answer()


# --------------------------------------------------------------------------- #
# Раздел «👥 Администраторы»
# --------------------------------------------------------------------------- #
@router.message(Command("admins", "adminlist"))
async def cmd_admins(message: Message, db: Database, bot: Bot, cfg: AppConfig) -> None:
    """Показать список администраторов клуба (кнопки — только владельцу)."""
    text, keyboard = await _render_admins(
        bot, db, cfg, viewer_id=message.from_user.id if message.from_user else None
    )
    await message.answer(truncate(text, MESSAGE_LIMIT), reply_markup=keyboard)
    logger.info("Список администраторов открыл %s", message.from_user.id if message.from_user else "?")


@router.callback_query(F.data == CB_ADMIN_ADMINS)
async def cb_admins_list(callback: CallbackQuery, db: Database, bot: Bot, cfg: AppConfig) -> None:
    """Кнопка «👥 Администраторы» / «🔄 Обновить»."""
    text, keyboard = await _render_admins(
        bot, db, cfg, viewer_id=callback.from_user.id if callback.from_user else None
    )
    await edit_or_send(
        bot,
        callback.message.chat.id,
        callback.message.message_id,
        truncate(text, MESSAGE_LIMIT),
        keyboard,
    )
    await callback.answer()


@router.callback_query(F.data == CB_ADMIN_ADD)
async def cb_admin_add_start(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
    admins: AdminRegistry | None = None,
) -> None:
    """Кнопка «➕ Добавить»: запросить ID нового администратора."""
    user_id = callback.from_user.id if callback.from_user else None
    if not await _is_owner(db, admins, user_id):
        await _deny_management(callback, db, user_id)
        return

    await state.set_state(AdminFSM.waiting_admin_id)
    await edit_or_send(
        bot,
        callback.message.chat.id,
        callback.message.message_id,
        texts.ADMIN_ADD_PROMPT,
        admin_add_cancel_keyboard(),
    )
    await callback.answer("Введите ID нового администратора")


@router.callback_query(F.data == CB_ADMIN_ADD_CANCEL)
async def cb_admin_add_cancel(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Кнопка «❌ Отменить» во время ввода ID."""
    await state.clear()
    await edit_or_send(
        bot,
        callback.message.chat.id,
        callback.message.message_id,
        texts.ADMIN_ADD_CANCELED,
        admins_nav_keyboard(),
    )
    await callback.answer("Добавление отменено")


@router.message(Command("cancel"), AdminFSM.waiting_admin_id)
async def cmd_cancel_admin(message: Message, state: FSMContext) -> None:
    """/cancel во время добавления администратора."""
    await state.clear()
    await message.answer(texts.ADMIN_ADD_CANCELED, reply_markup=admins_nav_keyboard())


@router.message(AdminFSM.waiting_admin_id, ~F.text.startswith("/"))
async def msg_admin_id(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Принять числовой Telegram ID нового администратора."""
    raw = (message.text or "").strip()
    if not _ADMIN_ID_RE.match(raw):
        await message.answer(texts.ADMIN_ADD_INVALID_ID)
        return

    admin_id = int(raw)
    operator_id = message.from_user.id if message.from_user else None
    await state.clear()

    existing = await db.get_admin(admin_id)
    label = await _admin_label(bot, db, admin_id)

    if existing is None:
        admin = await db.add_admin(admin_id, role=AdminRole.OPERATOR, added_by=operator_id)
        if admins is not None:
            admins.remember(admin_id)
        answer = texts.ADMIN_ADDED.format(user_link=label, user_id=admin.user_id, role=admin.role_title)
        if await db.get_user(admin_id) is None:
            answer += "\n\n" + texts.ADMIN_NOT_STARTED_WARNING.format(user_link=label)
        logger.info("Администратор %s добавлен пользователем %s", admin_id, operator_id)
    else:
        admin = await db.add_admin(
            admin_id,
            role=existing.role,
            added_by=operator_id or existing.added_by,
        )
        if admins is not None:
            admins.remember(admin_id)
        answer = texts.ADMIN_ALREADY_EXISTS.format(
            user_link=label,
            role=admin.role_title,
            state=texts.ADMINS_STATE_ACTIVE if admin.is_active else texts.ADMINS_STATE_DISABLED,
        )
        logger.info("Администратор %s уже был в списке — включён снова", admin_id)

    await message.answer(truncate(answer, MESSAGE_LIMIT))
    text, keyboard = await _render_admins(bot, db, cfg, viewer_id=operator_id)
    await message.answer(truncate(text, MESSAGE_LIMIT), reply_markup=keyboard)


# --------------------------------------------------------------------------- #
# Включение/отключение, смена роли, удаление администратора
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith(CB_ADMIN_TOGGLE_PREFIX))
async def cb_admin_toggle(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Кнопки «⛔️ Отключить» / «✅ Включить»."""
    user_id = callback.from_user.id if callback.from_user else None
    target = parse_admin_toggle(callback.data)
    if target is None:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    if not await _is_owner(db, admins, user_id):
        await _deny_management(callback, db, user_id)
        return

    admin = await db.get_admin(target)
    if admin is None:
        await _refresh_admins_view(bot, db, cfg, callback, "Администратор не найден — список обновлён")
        return
    if target == cfg.admin_id:
        await callback.answer(truncate(texts.ADMIN_OWNER_PROTECTED, CALLBACK_ANSWER_LIMIT), show_alert=True)
        return
    if target == user_id:
        await callback.answer(truncate(texts.ADMIN_SELF_PROTECTED, CALLBACK_ANSWER_LIMIT), show_alert=True)
        return
    if admin.is_owner and admin.is_active and await db.count_active_owners() <= 1:
        await callback.answer(
            truncate(texts.ADMIN_LAST_OWNER_PROTECTED, CALLBACK_ANSWER_LIMIT), show_alert=True
        )
        return

    make_active = not admin.is_active
    await db.set_admin_active(target, make_active)
    if admins is not None:
        admins.remember(target) if make_active else admins.forget(target)

    label = await _admin_label(bot, db, target)
    note = (texts.ADMIN_ENABLED if make_active else texts.ADMIN_DISABLED).format(user_link=label)
    logger.info(
        "Администратор %s %s пользователем %s",
        target,
        "включён" if make_active else "отключён",
        user_id,
    )
    await _refresh_admins_view(bot, db, cfg, callback, note)


@router.callback_query(F.data.startswith((CB_ADMIN_PROMOTE_PREFIX, CB_ADMIN_DEMOTE_PREFIX)))
async def cb_admin_role(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Кнопки «👑 Владелец» / «👤 Оператор» — смена роли."""
    user_id = callback.from_user.id if callback.from_user else None
    parsed = parse_admin_role(callback.data)
    if parsed is None:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    new_role, target = parsed
    if not await _is_owner(db, admins, user_id):
        await _deny_management(callback, db, user_id)
        return

    admin = await db.get_admin(target)
    if admin is None:
        await _refresh_admins_view(bot, db, cfg, callback, "Администратор не найден — список обновлён")
        return
    if target == cfg.admin_id:
        await callback.answer(truncate(texts.ADMIN_OWNER_PROTECTED, CALLBACK_ANSWER_LIMIT), show_alert=True)
        return
    if admin.role == new_role:
        await _refresh_admins_view(bot, db, cfg, callback, f"Роль уже «{admin.role_title}»")
        return
    if new_role == AdminRole.OPERATOR and await db.count_active_owners() <= 1:
        await callback.answer(
            truncate(texts.ADMIN_LAST_OWNER_PROTECTED, CALLBACK_ANSWER_LIMIT), show_alert=True
        )
        return

    await db.set_admin_role(target, new_role)
    label = await _admin_label(bot, db, target)
    note = (
        texts.ADMIN_ROLE_PROMOTED if new_role == AdminRole.OWNER else texts.ADMIN_ROLE_DEMOTED
    ).format(user_link=label)
    logger.info("Администратор %s получил роль %s от %s", target, new_role, user_id)
    await _refresh_admins_view(bot, db, cfg, callback, note)


@router.callback_query(F.data.startswith(CB_ADMIN_DELETE_PREFIX))
async def cb_admin_delete(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Кнопка «🗑 Удалить» — убрать администратора из списка."""
    user_id = callback.from_user.id if callback.from_user else None
    target = parse_admin_delete(callback.data)
    if target is None:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    if not await _is_owner(db, admins, user_id):
        await _deny_management(callback, db, user_id)
        return

    admin = await db.get_admin(target)
    if admin is None:
        await _refresh_admins_view(bot, db, cfg, callback, "Администратор не найден — список обновлён")
        return
    if target == cfg.admin_id:
        await callback.answer(truncate(texts.ADMIN_OWNER_PROTECTED, CALLBACK_ANSWER_LIMIT), show_alert=True)
        return
    if target == user_id:
        await callback.answer(truncate(texts.ADMIN_SELF_PROTECTED, CALLBACK_ANSWER_LIMIT), show_alert=True)
        return
    if admin.is_owner and await db.count_active_owners() <= 1:
        await callback.answer(
            truncate(texts.ADMIN_LAST_OWNER_PROTECTED, CALLBACK_ANSWER_LIMIT), show_alert=True
        )
        return

    label = await _admin_label(bot, db, target)
    await db.remove_admin(target)
    if admins is not None:
        admins.forget(target)
    logger.info("Администратор %s удалён пользователем %s", target, user_id)
    await _refresh_admins_view(bot, db, cfg, callback, texts.ADMIN_REMOVED.format(user_link=label))


# --------------------------------------------------------------------------- #
# Решения по заявкам: подтвердить / отклонить
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith((CB_ADMIN_APPROVE_PREFIX, CB_ADMIN_REJECT_PREFIX)))
async def cb_decision(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
) -> None:
    """Обработать нажатие «✅ Подтвердить» или «❌ Отклонить».

    Решение принимает тот администратор, чей запрос первым дошёл до базы:
    ``set_status_if_pending`` меняет статус только у ещё не обработанной заявки.
    """
    parsed = parse_admin_decision(callback.data)
    if parsed is None:
        await callback.answer("Некорректная кнопка решения", show_alert=True)
        return
    status, booking_id = parsed
    approved = status == BookingStatus.APPROVED
    admin_id = callback.from_user.id if callback.from_user else None

    booking = await db.get_booking(booking_id)
    if booking is None:
        await callback.answer(f"Заявка №{booking_id} не найдена в базе", show_alert=True)
        return

    if not await db.set_status_if_pending(booking_id, status, decided_by=admin_id):
        # Заявку уже обработали — возможно, другой администратор.
        fresh = await db.get_booking(booking_id) or booking
        decider = await resolve_decided_by(bot, db, fresh.decided_by)
        await callback.answer(
            truncate(
                texts.ADMIN_ALREADY_PROCESSED.format(
                    booking_id=booking_id,
                    status=BookingStatus.title(fresh.status),
                )
                + f" Решение принял: {decider}.",
                CALLBACK_ANSWER_LIMIT,
            ),
            show_alert=True,
        )
        await _apply_admin_view(
            bot,
            db,
            cfg,
            callback,
            texts.ADMIN_ALREADY_PROCESSED_NOTE.format(
                status=BookingStatus.title(fresh.status),
                decider=escape_html(decider),
            ),
        )
        return

    delivered = await deliver_decision(bot, cfg, booking, approved)
    decided_by = await resolve_decided_by(bot, db, admin_id)

    decision_text = texts.ADMIN_DECISION_OK if approved else texts.ADMIN_DECISION_NO
    if not delivered:
        decision_text += " Клиенту уведомление не доставлено."
    await callback.answer(
        truncate(decision_text.format(booking_id=booking_id), CALLBACK_ANSWER_LIMIT)
    )

    await _apply_admin_view(
        bot,
        db,
        cfg,
        callback,
        decision_note(cfg, approved, delivered, decided_by),
    )

    # Остальным администраторам гасим кнопки: решение уже принято.
    fresh = await db.get_booking(booking_id) or booking
    await sync_peer_admins(bot, db, cfg, fresh, exclude=admin_id)

    logger.info(
        "Бронь №%s %s администратором %s (%s), клиент уведомлён: %s",
        booking.id,
        "подтверждена" if approved else "отклонена",
        admin_id,
        decided_by,
        delivered,
    )


__all__ = ["router"]
