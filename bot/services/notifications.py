"""Отправка уведомлений клиенту и администраторам клуба.

Поддерживает:
* рассылку новых броней (с фото или PDF чека, точкой, кол-вом гостей и комментарием);
* персональные настройки уведомлений каждого админа:
  - ``notify_new``    — пуш при новой брони,
  - ``notify_cancel`` — пуш при отмене гостем,
  - ``notify_noshow`` — пуш при отметке «гость не пришёл»,
  - ``quiet_start``..``quiet_end`` — тихие часы («не будить ночью»),
  - фильтр по закреплённой точке (``admin.branch_id``) и защиту ``viewer`` без кнопок действий;
* уведомления гостю при подтверждении («Готово, ждём в …» + адрес + напоминание),
  отклонении (причина + комментарий + свободные альтернативы), изменении брони и
  отмене после подтверждения с предложением перенести визит.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import Chat

from bot import texts
from bot.config import AppConfig
from bot.db.database import Database
from bot.db.models import Admin, Booking, BookingStatus, RejectReason, User
from bot.keyboards.inline import booking_decision_keyboard
from bot.services.schedule import parse_hhmm
from bot.utils.formatting import escape_html, format_datetime, now_full_text, truncate

logger = logging.getLogger(__name__)

NEW_BOOKING_AMOUNT_BY_PRICE: str = "по прайсу (сверьтесь с прайс-листом)"

CAPTION_LIMIT: int = 1024
MESSAGE_LIMIT: int = 4096
CALLBACK_ANSWER_LIMIT: int = 200


@dataclass(slots=True)
class DeliveryReport:
    """Итог рассылки новой брони администраторам."""

    delivered: tuple[int, ...] = ()
    failed: tuple[int, ...] = ()
    notifications: list[tuple[int, int]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.delivered)

    @property
    def total(self) -> int:
        return len(self.delivered) + len(self.failed)

    def summary(self) -> str:
        return f"доставлено {len(self.delivered)}/{self.total}"


def is_in_quiet_hours(admin: Admin, now_dt: datetime) -> bool:
    """Проверить, попадает ли текущее время в «тихие часы» администратора."""
    if not admin.quiet_start or not admin.quiet_end:
        return False
    q_s = parse_hhmm(admin.quiet_start)
    q_e = parse_hhmm(admin.quiet_end)
    if q_s is None or q_e is None or q_s == q_e:
        return False
    cur = now_dt.hour * 60 + now_dt.minute
    if q_s < q_e:
        return q_s <= cur < q_e
    return cur >= q_s or cur < q_e


async def admin_recipients(
    db: Database,
    cfg: AppConfig,
    *,
    event_type: str = "new",
    branch_id: int | None = None,
    now_dt: datetime | None = None,
) -> tuple[int, ...]:
    """Кому отправлять пуш-уведомление с учётом роли, точки, типа пуша и тихих часов."""
    moment = now_dt or datetime.now(cfg.timezone)
    try:
        active_admins = await db.list_admins(active_only=True)
    except Exception:
        logger.exception("Не удалось прочитать администраторов из базы")
        active_admins = []

    if not active_admins:
        logger.warning("В базе нет активных администраторов — беру список из .env")
        return tuple(cfg.admin_ids)

    matched: list[int] = []
    for adm in active_admins:
        if (
            branch_id is not None
            and not adm.is_owner
            and adm.branch_id is not None
            and adm.branch_id != branch_id
        ):
            continue
        if event_type == "new" and not adm.notify_new:
            continue
        if event_type == "cancel" and not adm.notify_cancel:
            continue
        if event_type == "noshow" and not adm.notify_noshow:
            continue
        if is_in_quiet_hours(adm, moment):
            logger.info(
                "Администратор %s сейчас в тихих часах (%s–%s) — ночной пуш пропущен",
                adm.user_id,
                adm.quiet_start,
                adm.quiet_end,
            )
            continue
        matched.append(adm.user_id)
    return tuple(matched)


async def _send_booking_copy(
    bot: Bot,
    admin_id: int,
    booking: Booking,
    caption: str,
    *,
    with_buttons: bool = True,
) -> int | None:
    """Отправить одну копию заявки администратору (фото, PDF или текст)."""
    markup = booking_decision_keyboard(booking.id) if with_buttons else None
    try:
        if booking.receipt_file_id and booking.receipt_kind == "document":
            send_doc = getattr(bot, "send_document", None)
            if send_doc is not None:
                message = await send_doc(
                    chat_id=admin_id,
                    document=booking.receipt_file_id,
                    caption=truncate(caption, CAPTION_LIMIT),
                    reply_markup=markup,
                )
            else:
                message = await bot.send_message(
                    chat_id=admin_id,
                    text=truncate(caption, MESSAGE_LIMIT),
                    reply_markup=markup,
                )
        elif booking.receipt_file_id:
            message = await bot.send_photo(
                chat_id=admin_id,
                photo=booking.receipt_file_id,
                caption=truncate(caption, CAPTION_LIMIT),
                reply_markup=markup,
            )
        else:
            message = await bot.send_message(
                chat_id=admin_id,
                text=truncate(caption, MESSAGE_LIMIT),
                reply_markup=markup,
            )
    except TelegramForbiddenError:
        logger.error(
            "Администратор %s заблокировал бота или не начинал диалог — заявка №%s не доставлена",
            admin_id,
            booking.id,
        )
        return None
    except TelegramAPIError:
        logger.exception(
            "Не удалось отправить заявку №%s администратору %s", booking.id, admin_id
        )
        return None
    return message.message_id


async def deliver_new_booking(
    bot: Bot,
    cfg: AppConfig,
    db: Database,
    booking: Booking,
    user: User | None = None,
) -> DeliveryReport:
    """Разослать новую заявку подходящим администраторам."""
    recipients = await admin_recipients(
        db, cfg, event_type="new", branch_id=booking.branch_id
    )
    if not recipients:
        return DeliveryReport()

    admins_map = {a.user_id: a for a in await db.list_admins(active_only=True)}
    client_link = (
        user.link if user is not None else f"ID: <code>{booking.user_id}</code>"
    )
    payment_note = (
        texts.NEW_BOOKING_RECEIPT_NOTE
        if booking.receipt_file_id
        else texts.NEW_BOOKING_SPOT_NOTE
    )

    amount = booking.quoted_price
    if amount is None:
        amount = texts.amount_for_booking(
            booking.tariff, booking.time_slot, datetime.now(cfg.timezone).date()
        )

    extra_lines: list[str] = []
    if booking.branch_id:
        branch = await db.get_branch(booking.branch_id)
        if branch is not None and len(await db.list_branches(active_only=True)) > 1:
            extra_lines.append(f"🏢 Точка: <b>{escape_html(branch.name)}</b>")
    if booking.guests_count > 1:
        extra_lines.append(f"👥 Гостей: <b>{booking.guests_count}</b>")
    if booking.seat_label:
        extra_lines.append(f"🖥 ПК / место: <b>{escape_html(booking.seat_label)}</b>")
    if booking.extras:
        extras_map = {
            e.key: e.title for e in await db.list_tariff_extras(active_only=False)
        }
        names = [extras_map.get(k, k) for k in booking.extras]
        extra_lines.append(f"✨ Доп. услуги: <b>{escape_html(', '.join(names))}</b>")
    if booking.guest_comment:
        extra_lines.append(
            f"💬 Комментарий: <i>{escape_html(booking.guest_comment)}</i>"
        )

    caption = texts.render(
        texts.NEW_BOOKING_CAPTION,
        cfg.club,
        booking_id=booking.id,
        tariff=escape_html(booking.tariff),
        time_slot=escape_html(booking.time_slot),
        client_name=escape_html(booking.client_name),
        user_link=client_link,
        created_at=format_datetime(booking.created_at, cfg.timezone),
        amount=f"<b>{amount} ₽</b>"
        if amount is not None
        else NEW_BOOKING_AMOUNT_BY_PRICE,
        payment_note=payment_note,
        admins_count=len(recipients),
    )
    if extra_lines:
        caption = caption + "\n" + "\n".join(extra_lines)

    delivered: list[int] = []
    failed: list[int] = []
    notifications: list[tuple[int, int]] = []

    for admin_id in recipients:
        adm = admins_map.get(admin_id)
        can_decide = adm.can_act if adm is not None else True
        message_id = await _send_booking_copy(
            bot, admin_id, booking, caption, with_buttons=can_decide
        )
        if message_id is None:
            failed.append(admin_id)
            continue
        delivered.append(admin_id)
        if can_decide:
            notifications.append((admin_id, message_id))

    return DeliveryReport(
        delivered=tuple(delivered),
        failed=tuple(failed),
        notifications=notifications,
    )


async def deliver_client_cancel_push(
    bot: Bot,
    cfg: AppConfig,
    db: Database,
    booking: Booking,
    user: User | None = None,
) -> int:
    """Пуш администраторам, когда гость сам отменил свою бронь."""
    recipients = await admin_recipients(
        db, cfg, event_type="cancel", branch_id=booking.branch_id
    )
    if not recipients:
        return 0
    client_link = user.link if user is not None else escape_html(booking.client_name)
    text = (
        f"🙋 <b>ГОСТЬ ОТМЕНИЛ БРОНЬ №{booking.id}</b>\n\n"
        f"• Гость: {escape_html(booking.client_name)} ({client_link})\n"
        f"• Зал: <b>{escape_html(booking.tariff)}</b>\n"
        f"• Время: <b>{escape_html(booking.time_slot)}</b>\n"
        f"• Причина: <i>отменил сам через бота</i>"
    )
    sent = 0
    for admin_id in recipients:
        try:
            await bot.send_message(chat_id=admin_id, text=truncate(text, MESSAGE_LIMIT))
            sent += 1
        except TelegramAPIError:
            logger.debug("Не удалось доставить пуш отмены админу %s", admin_id)
    return sent


async def deliver_noshow_push(
    bot: Bot,
    cfg: AppConfig,
    db: Database,
    booking: Booking,
    marked_by: str,
    exclude_admin_id: int | None = None,
) -> int:
    """Пуш администраторам при отметке «гость не пришёл»."""
    recipients = await admin_recipients(
        db, cfg, event_type="noshow", branch_id=booking.branch_id
    )
    text = (
        f"🚷 <b>НЕЯВКА ГОСТЯ · БРОНЬ №{booking.id}</b>\n\n"
        f"• Гость: <b>{escape_html(booking.client_name)}</b>\n"
        f"• Зал / место: <b>{escape_html(booking.tariff)}</b>"
        + (f" · {escape_html(booking.seat_label)}" if booking.seat_label else "")
        + f"\n• Слот: <b>{escape_html(booking.time_slot)}</b>\n"
        f"• Отметил: {escape_html(marked_by)}"
    )
    sent = 0
    for admin_id in recipients:
        if exclude_admin_id is not None and admin_id == exclude_admin_id:
            continue
        try:
            await bot.send_message(chat_id=admin_id, text=truncate(text, MESSAGE_LIMIT))
            sent += 1
        except TelegramAPIError:
            logger.debug("Не удалось доставить пуш неявки админу %s", admin_id)
    return sent


async def resolve_decided_by(bot: Bot, db: Database, user_id: int | None) -> str:
    if not user_id:
        return "неизвестно"

    adm = await db.get_admin(int(user_id))
    if adm is not None and adm.display_name:
        return adm.display_name

    stored = await db.get_user(int(user_id))
    if stored is not None and stored.mention:
        return stored.mention

    try:
        chat: Chat = await bot.get_chat(int(user_id))
    except TelegramAPIError:
        return f"ID {user_id}"

    username = getattr(chat, "username", None)
    if username:
        return f"@{username}"
    full_name = getattr(chat, "full_name", None) or getattr(chat, "first_name", None)
    return str(full_name) if full_name else f"ID {user_id}"


async def sync_peer_admins(
    bot: Bot,
    db: Database,
    cfg: AppConfig,
    booking: Booking,
    exclude: int | None = None,
) -> list[int]:
    try:
        peers = await db.list_notifications(booking.id)
    except Exception:
        logger.exception(
            "Не удалось прочитать копии уведомления заявки №%s", booking.id
        )
        return []
    if exclude is not None:
        peers = [
            (admin_id, message_id)
            for admin_id, message_id in peers
            if admin_id != exclude
        ]
    if not peers:
        return []

    decided_by = await resolve_decided_by(bot, db, booking.decided_by)
    note = texts.PEER_DECISION_NOTE.format(
        booking_id=booking.id,
        tariff=escape_html(booking.tariff),
        time_slot=escape_html(booking.time_slot),
        client_name=escape_html(booking.client_name),
        decision=BookingStatus.decision_title(booking.status),
        decided_by=escape_html(decided_by),
        decided_at=format_datetime(booking.decided_at, cfg.timezone),
    )

    failed: list[int] = []
    for admin_id, message_id in peers:
        try:
            await bot.edit_message_caption(
                chat_id=admin_id,
                message_id=message_id,
                caption=truncate(note, CAPTION_LIMIT),
                reply_markup=None,
            )
            continue
        except TelegramAPIError:
            pass
        try:
            await bot.send_message(chat_id=admin_id, text=truncate(note, MESSAGE_LIMIT))
        except TelegramAPIError:
            failed.append(admin_id)
    return failed


async def deliver_decision(
    bot: Bot,
    cfg: AppConfig,
    booking: Booking,
    approved: bool,
    *,
    db: Database | None = None,
    reason: str | None = None,
    comment: str | None = None,
    alternatives: Sequence[str] = (),
) -> bool:
    """Сообщить клиенту решение по его брони (подтверждение с адресом или отказ с альтернативами)."""
    if booking.user_id <= 0:
        return False

    address = cfg.club.address
    noshow_note = "Напоминание: бронь держится 15 минут от начала слота."
    if db is not None:
        branch = await db.get_branch(booking.branch_id)
        if branch is not None and branch.full_address:
            address = branch.full_address
        penalty = await db.get_setting("booking_rules_noshow_penalty")
        if penalty:
            noshow_note = penalty

    if approved:
        visit_time = booking.slot_start or booking.time_slot
        seat_part = (
            f" · {escape_html(booking.seat_label)}" if booking.seat_label else ""
        )
        guests_part = (
            f"\n👥 Гостей: <b>{booking.guests_count}</b>"
            if booking.guests_count > 1
            else ""
        )
        text = (
            texts.render(
                texts.CLIENT_APPROVED,
                cfg.club,
                tariff=escape_html(booking.tariff) + seat_part,
                time_slot=escape_html(booking.time_slot),
                booking_id=booking.id,
                client_name=escape_html(booking.client_name),
            )
            + f"\n🕒 <b>Готово, ждём в {escape_html(visit_time)}!</b>"
            + guests_part
            + f"\n📍 Адрес: <b>{escape_html(address)}</b>"
            + f"\nℹ️ {escape_html(noshow_note)}"
        )
    else:
        reason_code = reason or booking.reject_reason
        reason_str = RejectReason.title(reason_code) if reason_code else ""
        comment_str = comment or booking.reject_comment or ""
        extra_parts: list[str] = []
        if reason_str:
            extra_parts.append(f"Причина: <b>{escape_html(reason_str)}</b>.")
        if comment_str:
            extra_parts.append(f"Комментарий клуба: <i>{escape_html(comment_str)}</i>.")
        if alternatives:
            alt_joined = " или ".join(f"<b>{escape_html(a)}</b>" for a in alternatives)
            extra_parts.append(
                f"💡 Свободная альтернатива: есть {alt_joined} — выберите в меню!"
            )

        text = texts.render(
            texts.CLIENT_REJECTED,
            cfg.club,
            booking_id=booking.id,
            tariff=escape_html(booking.tariff),
            time_slot=escape_html(booking.time_slot),
        )
        if extra_parts:
            text += "\n\n" + "\n".join(extra_parts)

    try:
        await bot.send_message(
            chat_id=booking.user_id, text=truncate(text, MESSAGE_LIMIT)
        )
    except (TelegramForbiddenError, TelegramAPIError):
        logger.warning(
            "Не удалось доставить решение клиенту %s по брони №%s",
            booking.user_id,
            booking.id,
        )
        return False
    return True


async def deliver_booking_modified(
    bot: Bot,
    cfg: AppConfig,
    db: Database,
    booking: Booking,
) -> bool:
    """Уведомить гостя об изменении параметров брони администратором."""
    if booking.user_id <= 0:
        return False
    branch = await db.get_branch(booking.branch_id)
    addr = branch.full_address if branch is not None else cfg.club.address
    seat_info = (
        f" · <b>{escape_html(booking.seat_label)}</b>" if booking.seat_label else ""
    )
    text = (
        f"✏️ <b>Ваша бронь №{booking.id} изменена администратором</b>\n\n"
        f"• Зал / ПК: <b>{escape_html(booking.tariff)}</b>{seat_info}\n"
        f"• Дата и время: <b>{escape_html(booking.time_slot)}</b>\n"
        f"• Гостей: <b>{booking.guests_count}</b>\n"
        f"• Адрес: <b>{escape_html(addr)}</b>\n\n"
        f"Вопросы по записи: <b>{escape_html(cfg.club.admin_phone)}</b>."
    )
    try:
        await bot.send_message(
            chat_id=booking.user_id, text=truncate(text, MESSAGE_LIMIT)
        )
    except TelegramAPIError:
        return False
    return True


async def deliver_booking_canceled(
    bot: Bot,
    cfg: AppConfig,
    db: Database,
    booking: Booking,
    *,
    alternatives: Sequence[str] = (),
) -> bool:
    """Уведомить гостя об отмене подтверждённой брони и предложить перенести визит."""
    if booking.user_id <= 0:
        return False
    reason_str = RejectReason.title(booking.reject_reason)
    alt_line = ""
    if alternatives:
        alt_joined = " или ".join(f"<b>{escape_html(a)}</b>" for a in alternatives)
        alt_line = f"\n💡 Предлагаем перенести визит — сегодня свободно: {alt_joined}."
    comment_line = (
        f"\n💬 Комментарий: <i>{escape_html(booking.reject_comment)}</i>"
        if booking.reject_comment
        else ""
    )
    text = (
        f"🚫 <b>Бронь №{booking.id} отменена</b>\n\n"
        f"• Зал и время: <b>{escape_html(booking.tariff)}</b> · {escape_html(booking.time_slot)}\n"
        f"• Причина: <b>{escape_html(reason_str)}</b>"
        f"{comment_line}"
        f"{alt_line}\n\n"
        f"Вы можете сразу перенести запись через главное меню бота или по телефону "
        f"<b>{escape_html(cfg.club.admin_phone)}</b>."
    )
    try:
        await bot.send_message(
            chat_id=booking.user_id, text=truncate(text, MESSAGE_LIMIT)
        )
    except TelegramAPIError:
        return False
    return True


def decision_note(
    cfg: AppConfig, approved: bool, delivered: bool, decided_by: str
) -> str:
    template = (
        texts.ADMIN_DECISION_APPROVED if approved else texts.ADMIN_DECISION_REJECTED
    )
    return template.format(
        decided_at=now_full_text(cfg.timezone),
        decided_by=escape_html(decided_by),
        notify_status=texts.CLIENT_NOTIFIED_OK
        if delivered
        else texts.CLIENT_NOTIFIED_FAIL,
    )


__all__ = [
    "CALLBACK_ANSWER_LIMIT",
    "CAPTION_LIMIT",
    "MESSAGE_LIMIT",
    "NEW_BOOKING_AMOUNT_BY_PRICE",
    "DeliveryReport",
    "admin_recipients",
    "decision_note",
    "deliver_booking_canceled",
    "deliver_booking_modified",
    "deliver_client_cancel_push",
    "deliver_decision",
    "deliver_new_booking",
    "deliver_noshow_push",
    "is_in_quiet_hours",
    "resolve_decided_by",
    "sync_peer_admins",
]
