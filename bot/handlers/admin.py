"""Админ-панель: заявки, статистика и управление администраторами клуба.

Доступ к роутеру закрыт фильтром :class:`IsAdmin`: администратором считается
активная запись в таблице ``admins`` (список берётся из ``ADMIN_IDS`` в ``.env``
и расширяется через панель).

Панель открывается **кнопкой «🛠 Админ-панель» в главном меню** — эта кнопка
присылается только администраторам (см. :func:`bot.ui.screens.menu_keyboard`).
Команды ``/admin`` и ``/admins`` оставлены как запасные точки входа.

Вся навигация по экранам панели (статистика, активные брони, список и карточки
администраторов) идёт через общий :class:`bot.ui.navigator.Navigator`, поэтому
панель перерисовывает **одно и то же** сообщение, не плодя ленту в чате.
В этом модуле лежат действия, которые меняют данные:

* подтверждение / отклонение заявки (под фото чека и из списка броней);
* добавление нового администратора по Telegram ID;
* включение / отключение, смена роли и удаление администратора.

Заявки приходят сразу всем активным администраторам — решение принимает тот,
кто нажал первым; у остальных кнопки заменяются отметкой «обработано …».
"""

from __future__ import annotations

import logging
import re
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import texts
from bot.config import AppConfig
from bot.db.database import Database
from bot.db.models import AdminRole, BookingStatus
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
    parse_admin_decision,
    parse_admin_delete,
    parse_admin_list_page,
    parse_admin_role,
    parse_admin_toggle,
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
from bot.states import (
    DATA_SCREEN,
    DATA_SCREEN_MESSAGE,
    DATA_SCREEN_STACK,
    AdminFSM,
)
from bot.ui.navigator import Navigator
from bot.ui.screens import Screen
from bot.utils.formatting import escape_html, truncate
from bot.utils.telegram import safe_delete_message, safe_edit_message_caption

logger = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(IsAdmin(), IsPrivateChat())
router.callback_query.filter(IsAdmin(), IsPrivateChat())

#: Числовой Telegram ID: от 3 цифр, без @username и лишних символов.
_ADMIN_ID_RE = re.compile(r"^\d{3,}$")


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
    """Собрать навигатор под событие администратора."""
    if isinstance(event, CallbackQuery):
        return Navigator.from_callback(event, db=db, cfg=cfg, state=state, admins=admins)
    return Navigator.from_message(event, db=db, cfg=cfg, state=state, admins=admins)


async def _keep_navigation(state: FSMContext) -> dict[str, Any]:
    """Сбросить FSM-состояние ввода, сохранив экран и стек переходов."""
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


async def _admin_label(bot: Bot, db: Database, user_id: int) -> str:
    """Как показать администратора в уведомлениях: «@nickname», «Имя (ID: …)»."""
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
    cfg: AppConfig,
    admins: AdminRegistry | None,
    user_id: int | None,
) -> bool:
    """Может ли этот администратор управлять списком администраторов."""
    if not user_id:
        return False
    if admins is not None:
        return await admins.is_owner(user_id)
    admin = await db.get_admin(user_id)
    if admin is not None:
        return admin.is_owner
    total, _ = await db.count_admins()
    return total == 0 and user_id == cfg.admin_id


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


async def _apply_admin_view(
    bot: Bot,
    db: Database,
    cfg: AppConfig,
    state: FSMContext,
    callback: CallbackQuery,
    note: str,
    admins: AdminRegistry | None = None,
) -> None:
    """Обновить сообщение администратора после решения по заявке.

    * если это фото чека — дописываем отметку в подпись и убираем кнопки;
    * если это текстовый экран «Активные брони» — перерисовываем его (заявка
      исчезает из списка без нового сообщения).
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

    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    await navigator.show(Screen.ADMIN_PENDING, 0, push=False)


# --------------------------------------------------------------------------- #
# Команды-точки входа (/admin, /admins) и старые callback-и совместимости
# --------------------------------------------------------------------------- #
@router.message(Command("admin", "stats"))
async def cmd_admin(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Открыть экран админ-панели (запасной вход помимо кнопки в меню)."""
    await _keep_navigation(state)
    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    await state.update_data(**{DATA_SCREEN_STACK: [{"s": Screen.MENU, "a": ""}]})
    await navigator.show(Screen.ADMIN, push=False)
    logger.info(
        "Админ-панель открыта командой: %s",
        message.from_user.id if message.from_user else "?",
    )


@router.message(Command("admins", "adminlist"))
async def cmd_admins(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Открыть список администраторов клуба."""
    await _keep_navigation(state)
    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    await state.update_data(
        **{
            DATA_SCREEN_STACK: [
                {"s": Screen.MENU, "a": ""},
                {"s": Screen.ADMIN, "a": ""},
            ]
        }
    )
    await navigator.show(Screen.ADMIN_ADMINS, push=False)
    logger.info(
        "Список администраторов открыл %s",
        message.from_user.id if message.from_user else "?",
    )


@router.callback_query(F.data == CB_ADMIN_STATS)
async def cb_refresh_stats(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Обновить сводку админ-панели (в т. ч. со старых кнопок ``ad:stats``)."""
    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    await navigator.show(Screen.ADMIN, push=False)
    await navigator.answer("Сводка обновлена")


@router.callback_query(F.data.startswith(CB_ADMIN_LIST_PREFIX))
async def cb_pending_list(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Список активных заявок со старых кнопок ``ad:list:<page>``."""
    page = parse_admin_list_page(callback.data) or 0
    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    await navigator.show(Screen.ADMIN_PENDING, page, push=False)
    await navigator.answer()


@router.callback_query(F.data == CB_ADMIN_ADMINS)
async def cb_admins_list(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Список администраторов со старых кнопок ``ad:admins``."""
    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    await navigator.show(Screen.ADMIN_ADMINS, push=False)
    await navigator.answer()


# --------------------------------------------------------------------------- #
# Добавление администратора по ID
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == CB_ADMIN_ADD)
async def cb_admin_add_start(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Кнопка «➕ Добавить»: запросить ID нового администратора."""
    user_id = callback.from_user.id if callback.from_user else None
    if not await _is_owner(db, cfg, admins, user_id):
        await _deny_management(callback, db, user_id)
        return

    await state.set_state(AdminFSM.waiting_admin_id)
    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    await navigator.show(Screen.ADMIN_ADD)
    await navigator.answer("Введите ID нового администратора")


@router.callback_query(F.data == CB_ADMIN_ADD_CANCEL)
async def cb_admin_add_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Кнопка «✕ Отменить» во время ввода ID."""
    await _keep_navigation(state)
    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    await navigator.show(Screen.ADMIN_ADMINS, push=False, note=texts.ADMIN_ADD_CANCELED)
    await navigator.answer("Добавление отменено")


@router.message(Command("cancel"), AdminFSM.waiting_admin_id)
async def cmd_cancel_admin(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """/cancel во время добавления администратора."""
    await _keep_navigation(state)
    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    await navigator.show(Screen.ADMIN_ADMINS, push=False, note=texts.ADMIN_ADD_CANCELED)


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
    navigator = _navigate(message, db=db, cfg=cfg, state=state, admins=admins)
    raw = (message.text or "").strip()
    await safe_delete_message(bot, navigator.user_id, message.message_id)

    if not _ADMIN_ID_RE.match(raw):
        await navigator.show(Screen.ADMIN_ADD, push=False, note=texts.ADMIN_ADD_INVALID_ID)
        return

    admin_id = int(raw)
    operator_id = message.from_user.id if message.from_user else None
    await _keep_navigation(state)

    existing = await db.get_admin(admin_id)
    label = await _admin_label(bot, db, admin_id)

    if existing is None:
        admin = await db.add_admin(admin_id, role=AdminRole.OPERATOR, added_by=operator_id)
        if admins is not None:
            admins.remember(admin_id)
        note = texts.ADMIN_ADDED.format(user_link=label, user_id=admin.user_id, role=admin.role_title)
        if await db.get_user(admin_id) is None:
            note += "\n\n" + texts.ADMIN_NOT_STARTED_WARNING.format(user_link=label)
        logger.info("Администратор %s добавлен пользователем %s", admin_id, operator_id)
    else:
        admin = await db.add_admin(
            admin_id,
            role=existing.role,
            added_by=operator_id or existing.added_by,
        )
        if admins is not None:
            admins.remember(admin_id)
        note = texts.ADMIN_ALREADY_EXISTS.format(
            user_link=label,
            user_id=admin.user_id,
            role=admin.role_title,
            state=texts.ADMINS_STATE_ACTIVE if admin.is_active else texts.ADMINS_STATE_DISABLED,
        )
        logger.info("Администратор %s уже был в списке — включён снова", admin_id)

    await navigator.show(Screen.ADMIN_ADMINS, push=False, note=note)


# --------------------------------------------------------------------------- #
# Включение/отключение, смена роли, удаление администратора
# --------------------------------------------------------------------------- #
async def _refresh_after_manage(
    navigator: Navigator,
    target_id: int,
    note: str,
    *,
    deleted: bool = False,
) -> None:
    """После правки администратора остаёмся на том же экране (карточка / список)."""
    current = (await navigator.current()).get("s")
    if deleted or current != Screen.ADMIN_ADMIN:
        await navigator.show(Screen.ADMIN_ADMINS, push=False)
    else:
        await navigator.show(Screen.ADMIN_ADMIN, target_id, push=False)
    await navigator.answer(note)


@router.callback_query(F.data.startswith(CB_ADMIN_TOGGLE_PREFIX))
async def cb_admin_toggle(
    callback: CallbackQuery,
    state: FSMContext,
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
    if not await _is_owner(db, cfg, admins, user_id):
        await _deny_management(callback, db, user_id)
        return

    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    admin = await db.get_admin(target)
    if admin is None:
        await navigator.show(Screen.ADMIN_ADMINS, push=False)
        await navigator.answer("Администратор не найден — список обновлён")
        return
    if target == cfg.admin_id:
        await navigator.alert(
            texts.ADMIN_OWNER_PROTECTED.format(user_link=await _admin_label(bot, db, target))
        )
        return
    if target == user_id:
        await navigator.alert(texts.ADMIN_SELF_PROTECTED)
        return
    if admin.is_owner and admin.is_active and await db.count_active_owners() <= 1:
        await navigator.alert(texts.ADMIN_LAST_OWNER_PROTECTED)
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
    await _refresh_after_manage(navigator, target, note)


@router.callback_query(F.data.startswith((CB_ADMIN_PROMOTE_PREFIX, CB_ADMIN_DEMOTE_PREFIX)))
async def cb_admin_role(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Кнопки смены роли: «👑 Владелец» / «👤 Оператор»."""
    user_id = callback.from_user.id if callback.from_user else None
    parsed = parse_admin_role(callback.data)
    if parsed is None:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    new_role, target = parsed
    if not await _is_owner(db, cfg, admins, user_id):
        await _deny_management(callback, db, user_id)
        return

    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    admin = await db.get_admin(target)
    if admin is None:
        await navigator.show(Screen.ADMIN_ADMINS, push=False)
        await navigator.answer("Администратор не найден — список обновлён")
        return
    if target == cfg.admin_id:
        await navigator.alert(
            texts.ADMIN_OWNER_PROTECTED.format(user_link=await _admin_label(bot, db, target))
        )
        return
    if admin.role == new_role:
        await _refresh_after_manage(navigator, target, f"Роль уже «{admin.role_title}»")
        return
    if new_role == AdminRole.OPERATOR and await db.count_active_owners() <= 1:
        await navigator.alert(texts.ADMIN_LAST_OWNER_PROTECTED)
        return

    await db.set_admin_role(target, new_role)
    label = await _admin_label(bot, db, target)
    note = (
        texts.ADMIN_ROLE_PROMOTED if new_role == AdminRole.OWNER else texts.ADMIN_ROLE_DEMOTED
    ).format(user_link=label)
    logger.info("Администратор %s получил роль %s от %s", target, new_role, user_id)
    await _refresh_after_manage(navigator, target, note)


@router.callback_query(F.data.startswith(CB_ADMIN_DELETE_PREFIX))
async def cb_admin_delete(
    callback: CallbackQuery,
    state: FSMContext,
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
    if not await _is_owner(db, cfg, admins, user_id):
        await _deny_management(callback, db, user_id)
        return

    navigator = _navigate(callback, db=db, cfg=cfg, state=state, admins=admins)
    admin = await db.get_admin(target)
    if admin is None:
        await navigator.show(Screen.ADMIN_ADMINS, push=False)
        await navigator.answer("Администратор не найден — список обновлён")
        return
    if target == cfg.admin_id:
        await navigator.alert(
            texts.ADMIN_OWNER_PROTECTED.format(user_link=await _admin_label(bot, db, target))
        )
        return
    if target == user_id:
        await navigator.alert(texts.ADMIN_SELF_PROTECTED)
        return
    if admin.is_owner and await db.count_active_owners() <= 1:
        await navigator.alert(texts.ADMIN_LAST_OWNER_PROTECTED)
        return

    label = await _admin_label(bot, db, target)
    await db.remove_admin(target)
    if admins is not None:
        admins.forget(target)
    logger.info("Администратор %s удалён пользователем %s", target, user_id)
    await _refresh_after_manage(navigator, target, texts.ADMIN_REMOVED.format(user_link=label), deleted=True)


# --------------------------------------------------------------------------- #
# Решения по заявкам: подтвердить / отклонить
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith((CB_ADMIN_APPROVE_PREFIX, CB_ADMIN_REJECT_PREFIX)))
async def cb_decision(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
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
            state,
            callback,
            texts.ADMIN_ALREADY_PROCESSED_NOTE.format(
                status=BookingStatus.title(fresh.status),
                decider=escape_html(decider),
            ),
            admins,
        )
        return

    delivered = await deliver_decision(bot, cfg, booking, approved)
    decided_by = await resolve_decided_by(bot, db, admin_id)

    decision_text = texts.ADMIN_DECISION_OK if approved else texts.ADMIN_DECISION_NO
    if not delivered:
        decision_text += " Клиенту уведомление не доставлено."
    await callback.answer(truncate(decision_text.format(booking_id=booking_id), CALLBACK_ANSWER_LIMIT))

    await _apply_admin_view(
        bot,
        db,
        cfg,
        state,
        callback,
        decision_note(cfg, approved, delivered, decided_by),
        admins,
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
