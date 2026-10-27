"""Расширенные обработчики админ-панели (модули 1–9).

Подключается как вложенный роутер к ``bot.handlers.admin.router`` (уже защищён
фильтрами ``IsAdmin()`` и ``IsPrivateChat()``). Роль ``viewer`` («наблюдатель»)
получает строгий read-only доступ: может открывать экраны и чеки, настраивать
свои личные пуши, но не может менять брони, расписание, тарифы или состав команды.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import date, datetime
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import texts
from bot.config import AppConfig
from bot.db.database import Database
from bot.db.models import (
    AdminRole,
    Booking,
    BookingStatus,
    RejectReason,
)
from bot.keyboards.inline import (
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
)
from bot.services.admins import AdminRegistry
from bot.services.club_config import (
    admin_branch_scope,
    calculate_quote,
)
from bot.services.notifications import (
    CALLBACK_ANSWER_LIMIT,
    deliver_booking_canceled,
    deliver_booking_modified,
    deliver_decision,
    deliver_noshow_push,
    resolve_decided_by,
    sync_peer_admins,
)
from bot.services.schedule import (
    WEEKDAY_FULL,
    extract_slot_window,
    minutes_to_hhmm,
    parse_hours_range,
    suggest_alternatives,
)
from bot.states import (
    DATA_ADMIN_EDIT_FIELD,
    DATA_ADMIN_MANUAL,
    DATA_ADMIN_REJECT_REASON,
    DATA_ADMIN_SCHED_TARGET,
    DATA_ADMIN_SETTING_KEY,
    DATA_ADMIN_TARGET_BOOKING,
    DATA_ADMIN_TARGET_BRANCH,
    DATA_ADMIN_TARIFF_TARGET,
    DATA_SCREEN,
    DATA_SCREEN_MESSAGE,
    DATA_SCREEN_STACK,
    AdminFSM,
)
from bot.ui.navigator import Navigator
from bot.ui.screens import Screen
from bot.utils.formatting import escape_html, truncate
from bot.utils.telegram import safe_delete_message

logger = logging.getLogger(__name__)

manage_router = Router(name="admin_manage")

VIEWER_FORBIDDEN_ALERT: str = (
    "👁 У вашей роли «наблюдатель» доступ только на просмотр — действия отключены."
)
BRANCH_FORBIDDEN_ALERT: str = (
    "🏢 Эта бронь относится к другой точке сети — у вас доступ только к своему филиалу."
)


# --------------------------------------------------------------------------- #
# Общие проверки ролей и навигации
# --------------------------------------------------------------------------- #
def _nav(
    event: Message | CallbackQuery,
    *,
    db: Database,
    cfg: AppConfig,
    state: FSMContext,
    admins: AdminRegistry | None = None,
) -> Navigator:
    if isinstance(event, CallbackQuery):
        return Navigator.from_callback(
            event, db=db, cfg=cfg, state=state, admins=admins
        )
    return Navigator.from_message(event, db=db, cfg=cfg, state=state, admins=admins)


async def _keep_nav(state: FSMContext) -> dict[str, Any]:
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


async def _ensure_can_act(
    event: Message | CallbackQuery,
    db: Database,
    admins: AdminRegistry | None,
) -> bool:
    uid = event.from_user.id if event.from_user else None
    if not uid:
        return False
    if admins is not None:
        allowed = await admins.can_act(uid)
    else:
        rec = await db.get_admin(uid)
        allowed = rec.can_act if rec is not None else True
    if not allowed and isinstance(event, CallbackQuery):
        await event.answer(VIEWER_FORBIDDEN_ALERT, show_alert=True)
    return allowed


async def _ensure_owner(
    event: Message | CallbackQuery,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None,
) -> bool:
    uid = event.from_user.id if event.from_user else None
    if not uid:
        return False
    if admins is not None:
        ok = await admins.is_owner(uid)
    else:
        rec = await db.get_admin(uid)
        if rec is not None:
            ok = rec.is_owner
        else:
            total, _ = await db.count_admins()
            ok = total == 0 and uid == cfg.admin_id
    if not ok and isinstance(event, CallbackQuery):
        rec = await db.get_admin(uid)
        role = (
            rec.role_title if rec is not None else AdminRole.title(AdminRole.OPERATOR)
        )
        await event.answer(
            truncate(
                texts.ADMIN_MANAGE_FORBIDDEN.format(role=role), CALLBACK_ANSWER_LIMIT
            ),
            show_alert=True,
        )
    return ok


async def _ensure_booking_branch_access(
    callback: CallbackQuery,
    db: Database,
    booking: Booking,
) -> bool:
    uid = callback.from_user.id if callback.from_user else None
    rec = await db.get_admin(uid) if uid else None
    scope = admin_branch_scope(rec)
    if scope is not None and booking.branch_id != scope:
        await callback.answer(BRANCH_FORBIDDEN_ALERT, show_alert=True)
        return False
    return True


async def _booking_alternatives(
    db: Database,
    cfg: AppConfig,
    booking: Booking,
) -> tuple[str, ...]:
    now = datetime.now(cfg.timezone)
    iso_date = booking.booking_date or now.date().isoformat()
    desired = booking.slot_start
    if not desired:
        _, desired, _ = extract_slot_window(booking.time_slot, now)
    return await suggest_alternatives(
        db,
        iso_date,
        desired,
        branch_id=booking.branch_id or 1,
        guests_count=booking.guests_count,
    )


# --------------------------------------------------------------------------- #
# 1 & 2. Действия с карточкой брони: чек 1 тапом, отказ с причиной, отмена, неявка, правка
# --------------------------------------------------------------------------- #
@manage_router.callback_query(F.data.startswith(CB_ADMIN_RECEIPT_PREFIX))
async def cb_open_receipt(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
) -> None:
    """Открыть чек гостя (фото или PDF) в один тап из карточки брони."""
    raw = (callback.data or "")[len(CB_ADMIN_RECEIPT_PREFIX) :]
    try:
        booking_id = int(raw)
    except ValueError:
        await callback.answer("Некорректный номер брони", show_alert=True)
        return

    booking = await db.get_booking(booking_id)
    if booking is None or not booking.receipt_file_id:
        await callback.answer("К этой заявке чек не прикреплён", show_alert=True)
        return
    if not await _ensure_booking_branch_access(callback, db, booking):
        return

    cap = (
        f"🧾 Чек по брони <b>№{booking.id}</b> · "
        f"{escape_html(booking.client_name)} ({escape_html(booking.tariff)}, {escape_html(booking.time_slot)})"
    )
    if booking.receipt_kind == "document":
        await bot.send_document(
            chat_id=callback.from_user.id,
            document=booking.receipt_file_id,
            caption=cap,
        )
    else:
        await bot.send_photo(
            chat_id=callback.from_user.id,
            photo=booking.receipt_file_id,
            caption=cap,
        )
    await callback.answer("Чек открыт")


@manage_router.callback_query(F.data.startswith(CB_ADMIN_REJECT_REASON_PREFIX))
async def cb_choose_reject_reason(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Выбор причины отклонения → экран необязательного комментария."""
    if not await _ensure_can_act(callback, db, admins):
        return
    tail = (callback.data or "")[len(CB_ADMIN_REJECT_REASON_PREFIX) :]
    parts = tail.split(":", 1)
    if len(parts) != 2 or not parts[0].isdigit():
        await callback.answer("Некорректная причина", show_alert=True)
        return
    booking_id, reason = int(parts[0]), parts[1]
    booking = await db.get_booking(booking_id)
    if booking is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    if not await _ensure_booking_branch_access(callback, db, booking):
        return

    await state.update_data(
        {
            DATA_ADMIN_TARGET_BOOKING: booking_id,
            DATA_ADMIN_REJECT_REASON: reason,
        }
    )
    await state.set_state(AdminFSM.entering_reject_comment)
    nav = _nav(callback, db=db, cfg=cfg, state=state, admins=admins)
    await nav.show(Screen.ADMIN_BOOKING_REJECT, booking_id, push=False)
    await nav.answer("Добавьте комментарий или подтвердите кнопкой")


async def _finalize_reject_or_cancel(
    event: Message | CallbackQuery,
    *,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    state: FSMContext,
    admins: AdminRegistry | None,
    booking_id: int,
    reason: str,
    comment: str,
    cancel_mode: bool,
) -> None:
    nav = _nav(event, db=db, cfg=cfg, state=state, admins=admins)
    booking = await db.get_booking(booking_id)
    if booking is None:
        await _keep_nav(state)
        await nav.show(Screen.ADMIN, push=False, note="⚠️ Заявка не найдена.")
        return

    uid = event.from_user.id if event.from_user else None
    alts = await _booking_alternatives(db, cfg, booking)

    if cancel_mode:
        ok = await db.cancel_approved_booking(
            booking_id,
            decided_by=uid,
            reason=reason,
            comment=comment or None,
        )
        await _keep_nav(state)
        if not ok:
            await nav.show(
                Screen.ADMIN_BOOKING,
                booking_id,
                push=False,
                note="⚠️ Бронь уже отменена.",
            )
            return
        fresh = await db.get_booking(booking_id) or booking
        await deliver_booking_canceled(bot, cfg, db, fresh, alternatives=alts)
        await sync_peer_admins(bot, db, cfg, fresh, exclude=uid)
        await nav.show(
            Screen.ADMIN_BOOKING,
            booking_id,
            push=False,
            note=f"🚫 Бронь №{booking_id} отменена ({escape_html(fresh.reason_label)}). Гостю предложены свободные окна.",
        )
        return

    ok = await db.set_status_if_pending(
        booking_id,
        BookingStatus.REJECTED,
        decided_by=uid,
        reject_reason=reason,
        reject_comment=comment or None,
    )
    await _keep_nav(state)
    if not ok:
        await nav.show(
            Screen.ADMIN_BOOKING,
            booking_id,
            push=False,
            note="⚠️ Заявка уже была обработана.",
        )
        return
    fresh = await db.get_booking(booking_id) or booking
    await deliver_decision(
        bot,
        cfg,
        fresh,
        approved=False,
        db=db,
        reason=reason,
        comment=comment or None,
        alternatives=alts,
    )
    await sync_peer_admins(bot, db, cfg, fresh, exclude=uid)
    await nav.show(
        Screen.ADMIN_BOOKING,
        booking_id,
        push=False,
        note=f"❌ Заявка №{booking_id} отклонена ({escape_html(fresh.reason_label)}). Гостю отправлены альтернативы.",
    )


@manage_router.callback_query(F.data.startswith(CB_ADMIN_REJECT_CONFIRM_PREFIX))
async def cb_confirm_reject_no_comment(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_can_act(callback, db, admins):
        return
    raw = (callback.data or "")[len(CB_ADMIN_REJECT_CONFIRM_PREFIX) :]
    if not raw.isdigit():
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    data = await state.get_data()
    reason = str(data.get(DATA_ADMIN_REJECT_REASON) or RejectReason.NO_SEATS)
    await _finalize_reject_or_cancel(
        callback,
        db=db,
        bot=bot,
        cfg=cfg,
        state=state,
        admins=admins,
        booking_id=int(raw),
        reason=reason,
        comment="",
        cancel_mode=False,
    )
    await callback.answer("Заявка отклонена")


@manage_router.message(
    AdminFSM.entering_reject_comment, F.text, ~F.text.startswith("/")
)
async def msg_reject_comment(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_can_act(message, db, admins):
        return
    data = await state.get_data()
    booking_id = int(data.get(DATA_ADMIN_TARGET_BOOKING) or 0)
    reason = str(data.get(DATA_ADMIN_REJECT_REASON) or RejectReason.NO_SEATS)
    comment = " ".join((message.text or "").split())[:200]
    deleted = await safe_delete_message(bot, message.chat.id, message.message_id)
    _ = deleted
    await _finalize_reject_or_cancel(
        message,
        db=db,
        bot=bot,
        cfg=cfg,
        state=state,
        admins=admins,
        booking_id=booking_id,
        reason=reason,
        comment=comment,
        cancel_mode=False,
    )


@manage_router.callback_query(F.data.startswith(CB_ADMIN_CANCEL_REASON_PREFIX))
async def cb_choose_cancel_reason(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Выбор причины отмены уже подтверждённой брони."""
    if not await _ensure_can_act(callback, db, admins):
        return
    tail = (callback.data or "")[len(CB_ADMIN_CANCEL_REASON_PREFIX) :]
    parts = tail.split(":", 1)
    if len(parts) != 2 or not parts[0].isdigit():
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    booking_id, reason = int(parts[0]), parts[1]
    booking = await db.get_booking(booking_id)
    if booking is None:
        await callback.answer("Бронь не найдена", show_alert=True)
        return
    if not await _ensure_booking_branch_access(callback, db, booking):
        return

    await state.update_data(
        {
            DATA_ADMIN_TARGET_BOOKING: booking_id,
            DATA_ADMIN_REJECT_REASON: reason,
        }
    )
    await state.set_state(AdminFSM.entering_cancel_comment)
    nav = _nav(callback, db=db, cfg=cfg, state=state, admins=admins)
    await nav.show(Screen.ADMIN_BOOKING_CANCEL, booking_id, push=False)
    await nav.answer("Укажите комментарий или подтвердите отмену")


@manage_router.callback_query(F.data.startswith(CB_ADMIN_CANCEL_CONFIRM_PREFIX))
async def cb_confirm_cancel_no_comment(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_can_act(callback, db, admins):
        return
    raw = (callback.data or "")[len(CB_ADMIN_CANCEL_CONFIRM_PREFIX) :]
    if not raw.isdigit():
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    data = await state.get_data()
    reason = str(data.get(DATA_ADMIN_REJECT_REASON) or RejectReason.ADMIN_CANCELED)
    await _finalize_reject_or_cancel(
        callback,
        db=db,
        bot=bot,
        cfg=cfg,
        state=state,
        admins=admins,
        booking_id=int(raw),
        reason=reason,
        comment="",
        cancel_mode=True,
    )
    await callback.answer("Бронь отменена")


@manage_router.message(
    AdminFSM.entering_cancel_comment, F.text, ~F.text.startswith("/")
)
async def msg_cancel_comment(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_can_act(message, db, admins):
        return
    data = await state.get_data()
    booking_id = int(data.get(DATA_ADMIN_TARGET_BOOKING) or 0)
    reason = str(data.get(DATA_ADMIN_REJECT_REASON) or RejectReason.ADMIN_CANCELED)
    comment = " ".join((message.text or "").split())[:200]
    await safe_delete_message(bot, message.chat.id, message.message_id)
    await _finalize_reject_or_cancel(
        message,
        db=db,
        bot=bot,
        cfg=cfg,
        state=state,
        admins=admins,
        booking_id=booking_id,
        reason=reason,
        comment=comment,
        cancel_mode=True,
    )


@manage_router.callback_query(F.data.startswith(CB_ADMIN_NOSHOW_PREFIX))
async def cb_mark_noshow(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Отметить «гость не пришёл» + отправить пуш коллегам/владельцу."""
    if not await _ensure_can_act(callback, db, admins):
        return
    raw = (callback.data or "")[len(CB_ADMIN_NOSHOW_PREFIX) :]
    if not raw.isdigit():
        await callback.answer("Некорректный номер брони", show_alert=True)
        return
    booking_id = int(raw)
    booking = await db.get_booking(booking_id)
    if booking is None:
        await callback.answer("Бронь не найдена", show_alert=True)
        return
    if not await _ensure_booking_branch_access(callback, db, booking):
        return

    uid = callback.from_user.id if callback.from_user else None
    await db.mark_booking_noshow(booking_id, decided_by=uid)
    fresh = await db.get_booking(booking_id) or booking
    who = await resolve_decided_by(bot, db, uid)
    await deliver_noshow_push(bot, cfg, db, fresh, marked_by=who, exclude_admin_id=uid)

    nav = _nav(callback, db=db, cfg=cfg, state=state, admins=admins)
    await nav.show(
        Screen.ADMIN_BOOKING,
        booking_id,
        push=False,
        note=f"🚷 По брони №{booking_id} зафиксирована неявка гостя.",
    )
    await callback.answer("Отмечено: гость не пришёл")


@manage_router.callback_query(F.data.startswith(CB_ADMIN_EDIT_PREFIX))
async def cb_edit_booking_action(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    """Изменить бронь: количество гостей / зал в 1 клик или ввод нового времени / № ПК."""
    if not await _ensure_can_act(callback, db, admins):
        return
    tail = (callback.data or "")[len(CB_ADMIN_EDIT_PREFIX) :]
    parts = tail.split(":")
    if len(parts) < 2 or not parts[0].isdigit():
        await callback.answer("Некорректная команда", show_alert=True)
        return
    booking_id = int(parts[0])
    action = parts[1]
    booking = await db.get_booking(booking_id)
    if booking is None:
        await callback.answer("Бронь не найдена", show_alert=True)
        return
    if not await _ensure_booking_branch_access(callback, db, booking):
        return

    nav = _nav(callback, db=db, cfg=cfg, state=state, admins=admins)

    if action == "gset" and len(parts) == 3 and parts[2].isdigit():
        new_guests = max(1, min(20, int(parts[2])))
        updated = await db.update_booking_details(booking_id, guests_count=new_guests)
        if updated is not None:
            await deliver_booking_modified(bot, cfg, db, updated)
        await nav.show(
            Screen.ADMIN_BOOKING_EDIT,
            booking_id,
            push=False,
            note=f"✅ Количество гостей обновлено: <b>{new_guests} чел.</b> (гость уведомлён).",
        )
        await callback.answer("Гости обновлены")
        return

    if action == "tf" and len(parts) == 3:
        rec = await db.get_tariff_record(parts[2])
        if rec is None:
            await callback.answer("Тариф не найден", show_alert=True)
            return
        updated = await db.update_booking_details(booking_id, tariff=rec.title)
        if updated is not None:
            await deliver_booking_modified(bot, cfg, db, updated)
        await nav.show(
            Screen.ADMIN_BOOKING_EDIT,
            booking_id,
            push=False,
            note=f"✅ Зал обновлён на <b>{escape_html(rec.title)}</b> (гость уведомлён).",
        )
        await callback.answer("Зал обновлён")
        return

    if action in ("time", "seat"):
        await state.update_data(
            {
                DATA_ADMIN_TARGET_BOOKING: booking_id,
                DATA_ADMIN_EDIT_FIELD: action,
            }
        )
        await state.set_state(AdminFSM.editing_booking_field)
        prompt = (
            "✍️ Введите новое время сессии (например <code>20:00–23:00</code> или <code>Ночь (22:00–08:00)</code>):"
            if action == "time"
            else "✍️ Введите номер ПК / место (например <code>ПК #7</code> или <code>VIP-комната #2</code>):"
        )
        await nav.show(Screen.ADMIN_BOOKING_EDIT, booking_id, push=False, note=prompt)
        await callback.answer("Ожидаю ввод в чат")


@manage_router.message(AdminFSM.editing_booking_field, F.text, ~F.text.startswith("/"))
async def msg_edit_booking_field(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_can_act(message, db, admins):
        return
    data = await state.get_data()
    booking_id = int(data.get(DATA_ADMIN_TARGET_BOOKING) or 0)
    field = str(data.get(DATA_ADMIN_EDIT_FIELD) or "")
    value = " ".join((message.text or "").split())[:60]

    nav = _nav(message, db=db, cfg=cfg, state=state, admins=admins)
    deleted = await safe_delete_message(bot, message.chat.id, message.message_id)
    nav.on_input_deleted(deleted)

    booking = await db.get_booking(booking_id)
    if booking is None or not value:
        await _keep_nav(state)
        await nav.show(Screen.ADMIN, push=False, note="⚠️ Бронь не найдена.")
        return

    if field == "time":
        iso_d, s_start, s_end = extract_slot_window(value, datetime.now(cfg.timezone))
        updated = await db.update_booking_details(
            booking_id,
            time_slot=value,
            booking_date=iso_d,
            slot_start=s_start or booking.slot_start,
            slot_end=s_end or booking.slot_end,
        )
        summary = f"Новое время: {value}"
    else:
        updated = await db.update_booking_details(booking_id, seat_label=value)
        summary = f"Назначено место: {value}"

    await _keep_nav(state)
    if updated is not None:
        await deliver_booking_modified(bot, cfg, db, updated)
    await nav.show(
        Screen.ADMIN_BOOKING,
        booking_id,
        push=False,
        note=f"✅ Бронь №{booking_id} обновлена ({escape_html(summary)}). Гость уведомлён!",
    )


# --------------------------------------------------------------------------- #
# 3. Ручное создание брони за гостя (звонок / ВК / стойка)
# --------------------------------------------------------------------------- #
@manage_router.callback_query(F.data.startswith(CB_ADMIN_MANUAL_PREFIX))
async def cb_manual_booking(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_can_act(callback, db, admins):
        return
    tail = (callback.data or "")[len(CB_ADMIN_MANUAL_PREFIX) :]
    nav = _nav(callback, db=db, cfg=cfg, state=state, admins=admins)
    uid = callback.from_user.id if callback.from_user else 0
    adm = await db.get_admin(uid)
    scope_branch = admin_branch_scope(adm)

    data = await state.get_data()
    draft: dict[str, Any] = dict(data.get(DATA_ADMIN_MANUAL) or {})

    if tail == "cancel":
        await state.set_state(None)
        await state.update_data({DATA_ADMIN_MANUAL: {}})
        await nav.show(Screen.ADMIN, push=False)
        await callback.answer("Создание брони отменено")
        return

    if tail == "start":
        branches = await db.list_branches(active_only=True)
        if scope_branch is not None:
            draft = {"branch_id": scope_branch, "step": "tariff"}
        elif len(branches) >= 2:
            draft = {"branch_id": branches[0].id, "step": "branch"}
        else:
            draft = {"branch_id": branches[0].id if branches else 1, "step": "tariff"}
        await state.set_state(None)
        await state.update_data({DATA_ADMIN_MANUAL: draft})
        await nav.show(Screen.ADMIN_MANUAL)
        await callback.answer()
        return

    if tail.startswith("br:"):
        br_id = int(tail.split(":", 1)[1])
        draft["branch_id"] = br_id
        draft["step"] = "tariff"
        await state.update_data({DATA_ADMIN_MANUAL: draft})
        await nav.show(Screen.ADMIN_MANUAL, push=False)
        await callback.answer()
        return

    if tail.startswith("tf:"):
        t_key = tail.split(":", 1)[1]
        rec = await db.get_tariff_record(t_key)
        draft["tariff_key"] = t_key
        draft["tariff_title"] = rec.title if rec else t_key.upper()
        draft["step"] = "time"
        await state.update_data({DATA_ADMIN_MANUAL: draft})
        await nav.show(Screen.ADMIN_MANUAL, push=False)
        await callback.answer()
        return

    if tail.startswith("tm:"):
        code = tail.split(":", 1)[1]
        now = datetime.now(cfg.timezone)
        if code == "custom":
            await state.set_state(AdminFSM.manual_entering_time)
            await nav.show(
                Screen.ADMIN_MANUAL,
                push=False,
                note="✍️ Отправьте время сессии (например <code>19:00-22:00</code> или <code>2026-10-27 18:00-21:00</code>):",
            )
            await callback.answer("Введите дату/время")
            return
        presets = {
            "now": (
                f"Сейчас (с {now:%H:%M}, {now:%d.%m.%Y})",
                now.strftime("%H:%M"),
                minutes_to_hhmm(now.hour * 60 + now.minute + 120),
                "now",
            ),
            "18_21": ("18:00–21:00", "18:00", "21:00", "18:00 на 3 часа"),
            "20_23": ("20:00–23:00", "20:00", "23:00", "20:00 на 3 часа"),
            "night": (
                f"Ночь ({texts.NIGHT_SLOT})",
                "22:00",
                "08:00",
                texts.TIME_NIGHT_KEY,
            ),
        }
        slot_txt, s_start, s_end, t_key = presets.get(code, presets["18_21"])
        draft.update(
            {
                "booking_date": now.date().isoformat(),
                "time_slot": slot_txt,
                "slot_start": s_start,
                "slot_end": s_end,
                "time_key": t_key,
                "step": "guests",
            }
        )
        await state.update_data({DATA_ADMIN_MANUAL: draft})
        await nav.show(Screen.ADMIN_MANUAL, push=False)
        await callback.answer()
        return

    if tail.startswith("g:"):
        guests = max(1, min(20, int(tail.split(":", 1)[1])))
        draft["guests_count"] = guests
        draft["step"] = "guest"
        await state.update_data({DATA_ADMIN_MANUAL: draft})
        await state.set_state(AdminFSM.manual_entering_guest)
        await nav.show(Screen.ADMIN_MANUAL, push=False)
        await callback.answer("Введите имя и контакт гостя")


@manage_router.message(AdminFSM.manual_entering_time, F.text, ~F.text.startswith("/"))
async def msg_manual_time(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_can_act(message, db, admins):
        return
    raw = " ".join((message.text or "").split())[:60]
    nav = _nav(message, db=db, cfg=cfg, state=state, admins=admins)
    deleted = await safe_delete_message(bot, message.chat.id, message.message_id)
    nav.on_input_deleted(deleted)

    now = datetime.now(cfg.timezone)
    date_m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(.+)$", raw)
    if date_m:
        bdate, slot_part = date_m.group(1), date_m.group(2)
        _, s_start, s_end = extract_slot_window(slot_part, now)
    else:
        bdate, s_start, s_end = extract_slot_window(raw, now)

    data = await state.get_data()
    draft: dict[str, Any] = dict(data.get(DATA_ADMIN_MANUAL) or {})
    draft.update(
        {
            "booking_date": bdate,
            "time_slot": raw,
            "slot_start": s_start or "",
            "slot_end": s_end or "",
            "time_key": raw,
            "step": "guests",
        }
    )
    await state.set_state(None)
    await state.update_data({DATA_ADMIN_MANUAL: draft})
    await nav.show(Screen.ADMIN_MANUAL, push=False)


@manage_router.message(AdminFSM.manual_entering_guest, F.text, ~F.text.startswith("/"))
async def msg_manual_guest(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_can_act(message, db, admins):
        return
    raw = " ".join((message.text or "").split())
    nav = _nav(message, db=db, cfg=cfg, state=state, admins=admins)
    deleted = await safe_delete_message(bot, message.chat.id, message.message_id)
    nav.on_input_deleted(deleted)

    parts = [p.strip() for p in re.split(r"[|;/]", raw) if p.strip()]
    if not parts:
        await nav.show(
            Screen.ADMIN_MANUAL, push=False, note="⚠️ Укажите хотя бы имя гостя."
        )
        return

    guest_name = parts[0][:40]
    guest_contact = parts[1][:60] if len(parts) >= 2 else ""
    seat_label = parts[2][:40] if len(parts) >= 3 else ""

    target_uid: int | None = None
    if guest_contact.startswith("@"):
        found = await db.find_user_by_username(guest_contact)
        if found is not None:
            target_uid = found.user_id

    data = await state.get_data()
    draft: dict[str, Any] = dict(data.get(DATA_ADMIN_MANUAL) or {})
    tariff_key = str(draft.get("tariff_key") or "standart")
    tariff_title_str = str(draft.get("tariff_title") or "Standart")
    time_slot = str(draft.get("time_slot") or "По записи администратора")
    bdate_str = str(draft.get("booking_date") or db.today_iso())
    guests_count = int(draft.get("guests_count") or 1)
    branch_id = int(draft.get("branch_id") or 1)

    try:
        bdate = date.fromisoformat(bdate_str)
    except ValueError:
        bdate = datetime.now(cfg.timezone).date()

    quoted, _ = await calculate_quote(
        db,
        tariff_key_or_title=tariff_key,
        time_key_or_slot=str(draft.get("time_key") or time_slot),
        day=bdate,
        guests_count=guests_count,
    )

    uid = message.from_user.id if message.from_user else cfg.admin_id
    booking = await db.create_manual_booking(
        admin_id=uid,
        tariff=tariff_title_str,
        time_slot=time_slot,
        client_name=guest_name,
        branch_id=branch_id,
        booking_date=bdate_str,
        slot_start=str(draft.get("slot_start") or "") or None,
        slot_end=str(draft.get("slot_end") or "") or None,
        guests_count=guests_count,
        seat_label=seat_label or None,
        guest_contact=guest_contact or None,
        quoted_price=quoted,
        target_user_id=target_uid,
    )

    await _keep_nav(state)
    await nav.show(
        Screen.ADMIN_BOOKING,
        booking.id,
        push=False,
        note=(
            f"✅ Ручная бронь <b>№{booking.id}</b> создана (помечена «создана админом» "
            "и учтена отдельной строкой в статистике)."
        ),
    )


# --------------------------------------------------------------------------- #
# 4. Расписание, шаг слота, буфер уборки, праздники и занятые слоты (Sheets/IZI)
# --------------------------------------------------------------------------- #
@manage_router.callback_query(F.data.startswith(CB_ADMIN_SCHED_PREFIX))
async def cb_schedule_action(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_owner(callback, db, cfg, admins):
        return
    tail = (callback.data or "")[len(CB_ADMIN_SCHED_PREFIX) :]
    nav = _nav(callback, db=db, cfg=cfg, state=state, admins=admins)

    if tail.startswith("step:"):
        step = int(tail.split(":", 1)[1])
        await db.set_setting("slot_step_minutes", str(step))
        await nav.show(
            Screen.ADMIN_SCHEDULE,
            push=False,
            note=f"⏱ Шаг слота переключён на <b>{step} мин</b>.",
        )
        await callback.answer(f"Шаг слота: {step} мин")
        return

    if tail.startswith("buf:"):
        buf = int(tail.split(":", 1)[1])
        await db.set_setting("slot_buffer_minutes", str(buf))
        await nav.show(
            Screen.ADMIN_SCHEDULE,
            push=False,
            note=f"🧹 Буфер на уборку между сессиями: <b>{buf} мин</b>.",
        )
        await callback.answer(f"Буфер: {buf} мин")
        return

    if tail.startswith("del:busy:"):
        slot_id = int(tail.split(":")[-1])
        await db.delete_external_busy_slot(slot_id)
        await nav.show(
            Screen.ADMIN_SCHEDULE, push=False, note="✅ Внешняя блокировка слота снята."
        )
        await callback.answer("Блокировка снята")
        return

    await state.update_data({DATA_ADMIN_SCHED_TARGET: tail})
    await state.set_state(AdminFSM.editing_schedule)

    if tail == "max":
        prompt = "✍️ Введите <b>максимум человек в одном слоте</b> (например <code>15</code>):"
    elif tail.startswith("day:"):
        wd = int(tail.split(":", 1)[1])
        prompt = (
            f"✍️ Часы работы для <b>{WEEKDAY_FULL[wd]}</b>:\n"
            "Отправьте интервал <code>10:00-23:00</code>, <code>00:00-24:00</code> (круглосуточно) или слово <code>выходной</code>."
        )
    elif tail == "hol":
        prompt = (
            "✍️ <b>Праздничный день:</b> отправьте дату и режим:\n"
            "• <code>2026-12-31 10:00-18:00 Канун Нового года</code>\n"
            "• <code>2027-01-01 выходной Новый год</code>"
        )
    else:
        prompt = (
            "✍️ <b>Блокировка занятого слота (Google Sheets / IZI):</b>\n"
            "Отправьте <code>ЧЧ:ММ-ЧЧ:ММ места источник</code> (например <code>18:00-21:00 5 IZI</code> "
            "или <code>2026-10-26 19:00-22:00 10 Sheets</code>)."
        )
    await nav.show(Screen.ADMIN_SCHEDULE, push=False, note=prompt)
    await callback.answer("Ожидаю ввод в чат")


@manage_router.message(AdminFSM.editing_schedule, F.text, ~F.text.startswith("/"))
async def msg_schedule_edit(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_owner(message, db, cfg, admins):
        return
    data = await state.get_data()
    target = str(data.get(DATA_ADMIN_SCHED_TARGET) or "")
    raw = " ".join((message.text or "").split())

    nav = _nav(message, db=db, cfg=cfg, state=state, admins=admins)
    deleted = await safe_delete_message(bot, message.chat.id, message.message_id)
    nav.on_input_deleted(deleted)

    if target == "max":
        if not raw.isdigit() or not (1 <= int(raw) <= 200):
            await nav.show(
                Screen.ADMIN_SCHEDULE, push=False, note="⚠️ Введите число от 1 до 200."
            )
            return
        await db.set_setting("slot_max_guests", raw)
        await _keep_nav(state)
        await nav.show(
            Screen.ADMIN_SCHEDULE,
            push=False,
            note=f"✅ Лимит слота обновлён: <b>{raw} чел.</b>",
        )
        return

    if target.startswith("day:"):
        wd = int(target.split(":", 1)[1])
        low = raw.lower()
        if low in ("выходной", "закрыто", "off", "-"):
            await db.set_schedule_day(wd, "00:00", "00:00", is_closed=True)
            note = f"✅ {WEEKDAY_FULL[wd]} отмечен как <b>выходной</b>."
        elif low in ("24/7", "24", "круглосуточно"):
            await db.set_schedule_day(wd, "00:00", "24:00", is_closed=False)
            note = f"✅ {WEEKDAY_FULL[wd]}: <b>круглосуточно (00:00–24:00)</b>."
        else:
            parsed = parse_hours_range(raw)
            if parsed is None:
                await nav.show(
                    Screen.ADMIN_SCHEDULE,
                    push=False,
                    note="⚠️ Формат часов: <code>10:00-23:00</code> или <code>выходной</code>.",
                )
                return
            await db.set_schedule_day(wd, parsed[0], parsed[1], is_closed=False)
            note = f"✅ {WEEKDAY_FULL[wd]}: <b>{parsed[0]}–{parsed[1]}</b>."
        await _keep_nav(state)
        await nav.show(Screen.ADMIN_SCHEDULE, push=False, note=note)
        return

    if target == "hol":
        tokens = raw.split(maxsplit=2)
        if len(tokens) < 2 or not re.match(r"^\d{4}-\d{2}-\d{2}$", tokens[0]):
            await nav.show(
                Screen.ADMIN_SCHEDULE,
                push=False,
                note="⚠️ Формат: <code>2026-12-31 10:00-18:00 Праздник</code> или <code>2027-01-01 выходной</code>.",
            )
            return
        h_date, spec = tokens[0], tokens[1]
        title = tokens[2] if len(tokens) == 3 else "Праздничный день"
        if spec.lower() in ("выходной", "закрыто", "off"):
            await db.upsert_holiday(h_date, title=title, is_closed=True)
        else:
            parsed = parse_hours_range(spec)
            if parsed is None:
                await nav.show(
                    Screen.ADMIN_SCHEDULE, push=False, note="⚠️ Неверный интервал часов."
                )
                return
            await db.upsert_holiday(
                h_date,
                title=title,
                open_time=parsed[0],
                close_time=parsed[1],
                is_closed=False,
            )
        await _keep_nav(state)
        await nav.show(
            Screen.ADMIN_SCHEDULE,
            push=False,
            note=f"🎉 Праздничный режим на <b>{h_date}</b> сохранён.",
        )
        return

    # target == "busy"
    tokens = raw.split()
    slot_date = db.today_iso()
    idx = 0
    if tokens and re.match(r"^\d{4}-\d{2}-\d{2}$", tokens[0]):
        slot_date = tokens[0]
        idx = 1
    if len(tokens) <= idx:
        await nav.show(
            Screen.ADMIN_SCHEDULE,
            push=False,
            note="⚠️ Укажите интервал, например <code>18:00-21:00 5 IZI</code>.",
        )
        return
    rng = parse_hours_range(tokens[idx])
    if rng is None:
        await nav.show(
            Screen.ADMIN_SCHEDULE,
            push=False,
            note="⚠️ Неверный интервал времени (пример: <code>18:00-21:00</code>).",
        )
        return
    seats = (
        int(tokens[idx + 1])
        if len(tokens) > idx + 1 and tokens[idx + 1].isdigit()
        else 5
    )
    source = tokens[idx + 2] if len(tokens) > idx + 2 else "Sheets/IZI"
    await db.add_external_busy_slot(
        slot_date,
        rng[0],
        rng[1],
        seats_taken=seats,
        source=source[:24],
    )
    await _keep_nav(state)
    await nav.show(
        Screen.ADMIN_SCHEDULE,
        push=False,
        note=f"📊 Заблокировано {rng[0]}–{rng[1]} ({seats} мест, источник: {escape_html(source)}).",
    )


# --------------------------------------------------------------------------- #
# 5. Тарифы (будни / выходные) и доп. услуги
# --------------------------------------------------------------------------- #
@manage_router.callback_query(F.data.startswith(CB_ADMIN_TARIFF_ADM_PREFIX))
async def cb_tariff_admin(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_owner(callback, db, cfg, admins):
        return
    tail = (callback.data or "")[len(CB_ADMIN_TARIFF_ADM_PREFIX) :]
    nav = _nav(callback, db=db, cfg=cfg, state=state, admins=admins)

    if tail.startswith("ext:tg:"):
        ext_key = tail.split(":")[-1]
        cur = await db.toggle_tariff_extra(ext_key)
        if cur is None:
            await callback.answer("Опция не найдена", show_alert=True)
            return
        await nav.show(Screen.ADMIN_TARIFFS, push=False)
        await callback.answer("Статус опции обновлён")
        return

    await state.update_data({DATA_ADMIN_TARIFF_TARGET: tail})
    await state.set_state(AdminFSM.editing_tariff)

    if tail == "ext:add":
        await nav.show(
            Screen.ADMIN_TARIFFS,
            push=False,
            note="✍️ Отправьте новую опцию в формате <code>Название | цена | эмодзи</code> (например <code>Энергетик | 220 | ⚡️</code>):",
        )
    elif tail.startswith("ext:ed:"):
        ext_key = tail.split(":")[-1]
        await nav.show(
            Screen.ADMIN_TARIFFS,
            push=False,
            note=f"✍️ Введите новую цену в ₽ для опции <code>{escape_html(ext_key)}</code> (число от 0):",
        )
    elif tail.startswith("ed:"):
        _, t_key, mode = tail.split(":", 2)
        if mode == "pack":
            prompt = "✍️ Введите 3 числа через пробел: <code>Утро День Кол-во_ПК</code> (например <code>450 520 10</code>):"
        elif mode == "wd":
            prompt = "✍️ Цены <b>будней</b> (4 числа через пробел): <code>1ч 3ч 5ч ночь</code> (например <code>150 400 620 700</code>):"
        else:
            prompt = "✍️ Цены <b>выходных ПТ–ВС</b> (4 числа): <code>1ч 3ч 5ч ночь</code> (например <code>170 450 690 800</code>):"
        await nav.show(Screen.ADMIN_TARIFF_ITEM, t_key, push=False, note=prompt)
    await callback.answer("Ожидаю ввод в чат")


@manage_router.message(AdminFSM.editing_tariff, F.text, ~F.text.startswith("/"))
async def msg_tariff_edit(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_owner(message, db, cfg, admins):
        return
    data = await state.get_data()
    target = str(data.get(DATA_ADMIN_TARIFF_TARGET) or "")
    raw = " ".join((message.text or "").split())

    nav = _nav(message, db=db, cfg=cfg, state=state, admins=admins)
    deleted = await safe_delete_message(bot, message.chat.id, message.message_id)
    nav.on_input_deleted(deleted)

    if target == "ext:add":
        parts = [p.strip() for p in raw.split("|") if p.strip()]
        if len(parts) < 2 or not parts[1].isdigit():
            await nav.show(
                Screen.ADMIN_TARIFFS,
                push=False,
                note="⚠️ Формат: <code>Название | цена | эмодзи</code>.",
            )
            return
        key = f"ext_{secrets.token_hex(3)}"
        emoji = parts[2][:4] if len(parts) >= 3 else "✨"
        await db.upsert_tariff_extra(
            key, parts[0][:36], emoji=emoji, price=int(parts[1]), is_active=True
        )
        await _keep_nav(state)
        await nav.show(
            Screen.ADMIN_TARIFFS,
            push=False,
            note=f"✅ Добавлена опция <b>{escape_html(parts[0])}</b>.",
        )
        return

    if target.startswith("ext:ed:"):
        ext_key = target.split(":")[-1]
        if not raw.isdigit():
            await nav.show(
                Screen.ADMIN_TARIFFS, push=False, note="⚠️ Введите целое число в рублях."
            )
            return
        cur = await db.get_tariff_extra(ext_key)
        if cur is not None:
            await db.upsert_tariff_extra(
                cur.key,
                cur.title,
                emoji=cur.emoji,
                price=int(raw),
                is_active=cur.is_active,
            )
        await _keep_nav(state)
        await nav.show(
            Screen.ADMIN_TARIFFS,
            push=False,
            note=f"✅ Цена опции обновлена: <b>{raw} ₽</b>.",
        )
        return

    if target.startswith("ed:"):
        _, t_key, mode = target.split(":", 2)
        rec = await db.get_tariff_record(t_key)
        if rec is None:
            await _keep_nav(state)
            await nav.show(Screen.ADMIN_TARIFFS, push=False)
            return
        nums = [int(x) for x in re.findall(r"\d+", raw)]
        if mode == "pack" and len(nums) >= 3:
            await db.update_tariff_prices(
                t_key, morning=nums[0], day=nums[1], pcs=nums[2]
            )
        elif mode == "wd" and len(nums) >= 4:
            await db.update_tariff_prices(
                t_key, hour1=nums[0], hour3=nums[1], hour5=nums[2], night=nums[3]
            )
        elif mode == "we" and len(nums) >= 4:
            await db.update_tariff_prices(
                t_key,
                hour1_weekend=nums[0],
                hour3_weekend=nums[1],
                hour5_weekend=nums[2],
                night_weekend=nums[3],
            )
        else:
            await nav.show(
                Screen.ADMIN_TARIFF_ITEM,
                t_key,
                push=False,
                note="⚠️ Проверьте количество чисел через пробел.",
            )
            return

        await _keep_nav(state)
        await nav.show(
            Screen.ADMIN_TARIFF_ITEM,
            t_key,
            push=False,
            note="✅ Тариф сохранён и обновлён в калькуляторе.",
        )


# --------------------------------------------------------------------------- #
# 6. Точки сети (филиалы)
# --------------------------------------------------------------------------- #
@manage_router.callback_query(F.data.startswith(CB_ADMIN_BRANCH_ADM_PREFIX))
async def cb_branch_admin(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_owner(callback, db, cfg, admins):
        return
    tail = (callback.data or "")[len(CB_ADMIN_BRANCH_ADM_PREFIX) :]
    nav = _nav(callback, db=db, cfg=cfg, state=state, admins=admins)

    if tail == "add":
        await state.update_data({DATA_ADMIN_TARGET_BRANCH: 0})
        await state.set_state(AdminFSM.editing_branch)
        await nav.show(
            Screen.ADMIN_BRANCHES,
            push=False,
            note=(
                "✍️ <b>Новая точка сети:</b> отправьте через <code>|</code>:\n"
                "<code>Название | Город | Адрес | Часы | Телефон</code>\n"
                "Пример: <code>NOVA ARENA Север | Неоград | пр-т Космонавтов, 18 | 24/7 | +7 (900) 000-22-33</code>"
            ),
        )
        await callback.answer("Введите данные новой точки")
        return

    if tail.startswith("ed:"):
        br_id = int(tail.split(":", 1)[1])
        await state.update_data({DATA_ADMIN_TARGET_BRANCH: br_id})
        await state.set_state(AdminFSM.editing_branch)
        await nav.show(
            Screen.ADMIN_BRANCH_ITEM,
            br_id,
            push=False,
            note="✍️ Отправьте обновлённые поля через <code>|</code>: <code>Название | Город | Адрес | Часы | Телефон</code>",
        )
        await callback.answer("Введите новые данные филиала")
        return

    if tail.startswith("tg:"):
        br_id = int(tail.split(":", 1)[1])
        br = await db.get_branch(br_id)
        if br is None:
            await callback.answer("Точка не найдена", show_alert=True)
            return
        ok = await db.set_branch_active(br_id, not br.is_active)
        if not ok:
            await callback.answer(
                "Нельзя скрыть единственную активную точку клуба", show_alert=True
            )
            return
        await nav.show(Screen.ADMIN_BRANCH_ITEM, br_id, push=False)
        await callback.answer("Статус точки обновлён")


@manage_router.message(AdminFSM.editing_branch, F.text, ~F.text.startswith("/"))
async def msg_branch_edit(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_owner(message, db, cfg, admins):
        return
    data = await state.get_data()
    br_id = int(data.get(DATA_ADMIN_TARGET_BRANCH) or 0)
    raw = " ".join((message.text or "").split())

    nav = _nav(message, db=db, cfg=cfg, state=state, admins=admins)
    deleted = await safe_delete_message(bot, message.chat.id, message.message_id)
    nav.on_input_deleted(deleted)

    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3 or not parts[0] or not parts[2]:
        target_scr = Screen.ADMIN_BRANCH_ITEM if br_id else Screen.ADMIN_BRANCHES
        await nav.show(
            target_scr,
            br_id or "",
            push=False,
            note="⚠️ Укажите минимум 3 поля через <code>|</code>: <code>Название | Город | Адрес | Часы | Телефон</code>.",
        )
        return

    name, city, address = parts[0][:48], parts[1][:36], parts[2][:80]
    hours = parts[3][:32] if len(parts) >= 4 and parts[3] else "24/7"
    phone = parts[4][:32] if len(parts) >= 5 else ""

    await _keep_nav(state)
    if br_id == 0:
        created = await db.add_branch(
            name, city=city, address=address, hours=hours, phone=phone
        )
        await nav.show(
            Screen.ADMIN_BRANCHES,
            push=False,
            note=f"✅ Точка <b>{escape_html(created.name)}</b> добавлена!",
        )
    else:
        await db.update_branch(
            br_id, name=name, city=city, address=address, hours=hours, phone=phone
        )
        await nav.show(
            Screen.ADMIN_BRANCH_ITEM,
            br_id,
            push=False,
            note="✅ Данные филиала обновлены.",
        )


# --------------------------------------------------------------------------- #
# 7. Роли админов (owner/operator/viewer), привязка точки, инвайты и передача смены
# --------------------------------------------------------------------------- #
@manage_router.callback_query(F.data.startswith(CB_ADMIN_ROLE_PREFIX))
async def cb_set_admin_role_ext(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_owner(callback, db, cfg, admins):
        return
    tail = (callback.data or "")[len(CB_ADMIN_ROLE_PREFIX) :]
    parts = tail.split(":", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        await callback.answer("Некорректная роль", show_alert=True)
        return
    new_role = AdminRole.normalize(parts[0])
    target_id = int(parts[1])

    if target_id == cfg.admin_id:
        await callback.answer(
            "Главный владелец из .env защищён от смены роли", show_alert=True
        )
        return
    rec = await db.get_admin(target_id)
    if rec is None:
        await callback.answer("Сотрудник не найден", show_alert=True)
        return
    if (
        rec.is_owner
        and new_role != AdminRole.OWNER
        and await db.count_active_owners() <= 1
    ):
        await callback.answer(texts.ADMIN_LAST_OWNER_PROTECTED, show_alert=True)
        return

    await db.set_admin_role(target_id, new_role)
    if admins is not None and rec.is_active:
        admins.remember(target_id, role=new_role)

    nav = _nav(callback, db=db, cfg=cfg, state=state, admins=admins)
    await nav.show(
        Screen.ADMIN_ADMIN,
        target_id,
        push=False,
        note=f"✅ Назначена роль: <b>{escape_html(AdminRole.title(new_role))}</b>.",
    )
    await callback.answer("Роль обновлена")


@manage_router.callback_query(F.data.startswith(CB_ADMIN_ASSIGN_BRANCH_PREFIX))
async def cb_assign_admin_branch(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_owner(callback, db, cfg, admins):
        return
    tail = (callback.data or "")[len(CB_ADMIN_ASSIGN_BRANCH_PREFIX) :]
    parts = tail.split(":", 1)
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    br_id = int(parts[0])
    target_id = int(parts[1])
    await db.set_admin_branch(target_id, br_id if br_id > 0 else None)

    nav = _nav(callback, db=db, cfg=cfg, state=state, admins=admins)
    await nav.show(Screen.ADMIN_ADMIN, target_id, push=False)
    await callback.answer("Привязка точки обновлена")


@manage_router.callback_query(F.data.startswith(CB_ADMIN_INVITE_PREFIX))
async def cb_generate_invite(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_owner(callback, db, cfg, admins):
        return
    role = AdminRole.normalize((callback.data or "")[len(CB_ADMIN_INVITE_PREFIX) :])
    uid = callback.from_user.id if callback.from_user else None
    inv = await db.create_admin_invite(role=role, created_by=uid)

    bot_username = "your_bot"
    try:
        me = await bot.get_me()
        if getattr(me, "username", None):
            bot_username = me.username
    except (TelegramAPIError, AttributeError):
        logger.debug("Имя пользователя бота недоступно — использую шаблон")
    link = f"https://t.me/{bot_username}?start=adm_{inv.token}"

    nav = _nav(callback, db=db, cfg=cfg, state=state, admins=admins)
    await nav.show(
        Screen.ADMIN_ADMINS,
        push=False,
        note=(
            f"🔗 Одноразовая ссылка-приглашение (роль <b>{escape_html(AdminRole.title(role))}</b>):\n"
            f"<code>{escape_html(link)}</code>"
        ),
    )
    await callback.answer("Ссылка-приглашение создана")


@manage_router.callback_query(F.data.startswith(CB_ADMIN_HANDOVER_PREFIX))
async def cb_handover_branch(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_owner(callback, db, cfg, admins):
        return
    tail = (callback.data or "")[len(CB_ADMIN_HANDOVER_PREFIX) :]
    parts = tail.split(":", 1)
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await callback.answer("Некорректные данные передачи", show_alert=True)
        return
    from_uid, to_uid = int(parts[0]), int(parts[1])
    source = await db.get_admin(from_uid)
    br_id = (source.branch_id if source and source.branch_id else None) or 1
    await db.transfer_branch_admin(br_id, from_admin_id=from_uid, to_admin_id=to_uid)
    nav = _nav(callback, db=db, cfg=cfg, state=state, admins=admins)
    await nav.show(
        Screen.ADMIN_ADMIN,
        to_uid,
        push=False,
        note=f"🔄 Точка #{br_id} передана сотруднику ID <code>{to_uid}</code> (история смен сохранена).",
    )
    await callback.answer("Точка передана")


# --------------------------------------------------------------------------- #
# 8. Настройки уведомлений администратора и тихие часы
# --------------------------------------------------------------------------- #
@manage_router.callback_query(F.data.startswith(CB_ADMIN_NOTIFY_PREFIX))
async def cb_admin_notify_prefs(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    rec = await db.get_admin(uid)
    if rec is None:
        role = AdminRole.OWNER if uid == cfg.admin_id else AdminRole.OPERATOR
        rec = await db.add_admin(uid, role=role)

    tail = (callback.data or "")[len(CB_ADMIN_NOTIFY_PREFIX) :]
    nav = _nav(callback, db=db, cfg=cfg, state=state, admins=admins)

    if tail == "tg:new":
        await db.update_admin_notifications(uid, notify_new=not rec.notify_new)
    elif tail == "tg:cancel":
        await db.update_admin_notifications(uid, notify_cancel=not rec.notify_cancel)
    elif tail == "tg:noshow":
        await db.update_admin_notifications(uid, notify_noshow=not rec.notify_noshow)
    elif tail == "q:off":
        await db.update_admin_notifications(uid, quiet_start=None, quiet_end=None)
    elif tail == "q:23_08":
        await db.update_admin_notifications(uid, quiet_start="23:00", quiet_end="08:00")
    elif tail == "q:00_09":
        await db.update_admin_notifications(uid, quiet_start="00:00", quiet_end="09:00")
    elif tail == "q:custom":
        await state.set_state(AdminFSM.editing_quiet_hours)
        await nav.show(
            Screen.ADMIN_NOTIFY,
            push=False,
            note="✍️ Введите интервал тихих часов (например <code>23:30-07:30</code>):",
        )
        await callback.answer("Введите тихие часы")
        return

    await nav.show(Screen.ADMIN_NOTIFY, push=False)
    await callback.answer("Настройки уведомлений сохранены")


@manage_router.message(AdminFSM.editing_quiet_hours, F.text, ~F.text.startswith("/"))
async def msg_admin_quiet_hours(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    uid = message.from_user.id if message.from_user else 0
    raw = " ".join((message.text or "").split())
    nav = _nav(message, db=db, cfg=cfg, state=state, admins=admins)
    deleted = await safe_delete_message(bot, message.chat.id, message.message_id)
    nav.on_input_deleted(deleted)

    rng = parse_hours_range(raw)
    if rng is None or rng[1] == "24:00":
        await nav.show(
            Screen.ADMIN_NOTIFY,
            push=False,
            note="⚠️ Укажите интервал в формате <code>23:00-08:00</code>.",
        )
        return
    await db.update_admin_notifications(uid, quiet_start=rng[0], quiet_end=rng[1])
    await _keep_nav(state)
    await nav.show(
        Screen.ADMIN_NOTIFY,
        push=False,
        note=f"🌙 Режим тишины включён: <b>{rng[0]}–{rng[1]}</b>.",
    )


# --------------------------------------------------------------------------- #
# 9. Настройки клуба (название/адрес, ссылка на чат, реквизиты текст/фото, правила)
# --------------------------------------------------------------------------- #
@manage_router.callback_query(F.data.startswith(CB_ADMIN_SETTINGS_PREFIX))
async def cb_club_settings(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_owner(callback, db, cfg, admins):
        return
    tail = (callback.data or "")[len(CB_ADMIN_SETTINGS_PREFIX) :]
    nav = _nav(callback, db=db, cfg=cfg, state=state, admins=admins)

    if tail == "tg:cancel":
        cur = await db.get_setting("booking_rules_allow_cancel", "1")
        new_val = "0" if cur != "0" else "1"
        await db.set_setting("booking_rules_allow_cancel", new_val)
        await nav.show(Screen.ADMIN_SETTINGS, push=False)
        await callback.answer("Правило отмены обновлено")
        return

    if tail.startswith("ed:"):
        field = tail.split(":", 1)[1]
        await state.update_data({DATA_ADMIN_SETTING_KEY: field})
        await state.set_state(AdminFSM.editing_setting)
        prompts = {
            "identity": "✍️ Отправьте через <code>|</code>: <code>Название | Город | Адрес</code>",
            "chat_url": "✍️ Отправьте ссылку на общий чат (например <code>https://t.me/nova_arena_chat</code>) или <code>-</code> чтобы убрать:",
            "payment": (
                "✍️ Отправьте <b>фото/QR реквизитов</b> (можно с подписью) ИЛИ текст через <code>|</code>:\n"
                "<code>Система | Реквизиты | Получатель</code>"
            ),
            "welcome": "✍️ Отправьте новый приветственный текст для гостей (или <code>-</code> для стандартного):",
            "lead_hours": "✍️ За сколько часов минимум можно бронировать слот (число от <code>0</code> до <code>48</code>):",
            "noshow": "✍️ Отправьте текст правила / штрафа за неявку:",
        }
        await nav.show(
            Screen.ADMIN_SETTINGS,
            push=False,
            note=prompts.get(field, "✍️ Введите новое значение:"),
        )
        await callback.answer("Ожидаю ввод в чат")


@manage_router.message(AdminFSM.editing_setting, F.photo)
async def msg_setting_payment_photo(
    message: Message,
    state: FSMContext,
    db: Database,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_owner(message, db, cfg, admins):
        return
    data = await state.get_data()
    field = str(data.get(DATA_ADMIN_SETTING_KEY) or "")
    nav = _nav(message, db=db, cfg=cfg, state=state, admins=admins)
    if field != "payment" or not message.photo:
        await nav.show(
            Screen.ADMIN_SETTINGS,
            push=False,
            note="⚠️ Фото поддерживается только для поля «Реквизиты».",
        )
        return

    photo_id = message.photo[-1].file_id
    updates: dict[str, str] = {"payment_photo_file_id": photo_id}
    if message.caption:
        updates["payment_card"] = message.caption.strip()[:80]
    await db.set_settings(updates)
    await _keep_nav(state)
    await nav.show(
        Screen.ADMIN_SETTINGS,
        push=False,
        note="🖼 Фото / QR реквизитов сохранено — теперь гости могут открыть его на шаге оплаты.",
    )


@manage_router.message(AdminFSM.editing_setting, F.text, ~F.text.startswith("/"))
async def msg_setting_text(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    cfg: AppConfig,
    admins: AdminRegistry | None = None,
) -> None:
    if not await _ensure_owner(message, db, cfg, admins):
        return
    data = await state.get_data()
    field = str(data.get(DATA_ADMIN_SETTING_KEY) or "")
    raw = " ".join((message.text or "").split())

    nav = _nav(message, db=db, cfg=cfg, state=state, admins=admins)
    deleted = await safe_delete_message(bot, message.chat.id, message.message_id)
    nav.on_input_deleted(deleted)

    if field == "identity":
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 3:
            await nav.show(
                Screen.ADMIN_SETTINGS,
                push=False,
                note="⚠️ Укажите <code>Название | Город | Адрес</code>.",
            )
            return
        await db.set_settings(
            {
                "club_name": parts[0][:48],
                "club_city": parts[1][:36],
                "club_address": parts[2][:80],
            }
        )
        await db.update_branch(
            1, name=parts[0][:48], city=parts[1][:36], address=parts[2][:80]
        )
    elif field == "chat_url":
        val = "" if raw in ("-", "нет", "off") else raw[:120]
        await db.set_setting("chat_url", val)
    elif field == "payment":
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) >= 3:
            await db.set_settings(
                {
                    "payment_system": parts[0][:32],
                    "payment_card": parts[1][:64],
                    "payment_holder": parts[2][:48],
                }
            )
        else:
            await db.set_setting("payment_card", raw[:64])
    elif field == "welcome":
        val = "" if raw in ("-", "сброс") else raw[:260]
        await db.set_setting("welcome_text", val)
    elif field == "lead_hours":
        if not raw.isdigit() or not (0 <= int(raw) <= 72):
            await nav.show(
                Screen.ADMIN_SETTINGS,
                push=False,
                note="⚠️ Введите число часов от 0 до 72.",
            )
            return
        await db.set_setting("booking_rules_lead_hours", raw)
    elif field == "noshow":
        await db.set_setting("booking_rules_noshow_penalty", raw[:220])

    await _keep_nav(state)
    await nav.show(
        Screen.ADMIN_SETTINGS,
        push=False,
        note="✅ Настройки клуба сохранены и сразу применены.",
    )


__all__ = ["manage_router"]
