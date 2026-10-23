"""Админ-панель: уведомления о новых бронях, подтверждения и статистика.

Доступ к роутеру закрыт фильтром :class:`IsAdmin` (ID берётся из ``ADMIN_ID``
в ``.env``), поэтому команды и кнопки панели видит только администратор клуба.

Что умеет панель:

* ``/admin`` — статистика: всего заявок, ожидают, подтверждено, отклонено;
* «📋 Активные брони» — список ``pending``-заявок со страницами и кнопками
  «✅ Подтвердить» / «❌ Отклонить» по каждой;
* кнопки под фото чека — решение по конкретной заявке: статус пишется в базу
  (только если заявка ещё ``pending``), клиенту уходит уведомление, а сообщение
  администратора обновляется отметкой о решении.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot import texts
from bot.config import AppConfig
from bot.db.database import Database
from bot.db.models import BookingStatus
from bot.filters.access import IsAdmin, IsPrivateChat
from bot.keyboards.inline import (
    CB_ADMIN_APPROVE_PREFIX,
    CB_ADMIN_LIST_PREFIX,
    CB_ADMIN_REJECT_PREFIX,
    CB_ADMIN_STATS,
    PENDING_PAGE_SIZE,
    admin_panel_keyboard,
    parse_admin_decision,
    parse_admin_list_page,
    pending_list_keyboard,
)
from bot.services.notifications import (
    CAPTION_LIMIT,
    MESSAGE_LIMIT,
    deliver_decision,
)
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


# --------------------------------------------------------------------------- #
# Вспомогательные функции отрисовки
# --------------------------------------------------------------------------- #
async def _stats_text(db: Database) -> str:
    """Текст панели со статистикой (используется и /admin, и кнопка обновления)."""
    stats = await db.get_stats()
    return texts.ADMIN_PANEL_GREETING.format(
        club_name=texts.CLUB_NAME,
        stats=texts.STATS_TEMPLATE.format(**stats.as_dict()),
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


# --------------------------------------------------------------------------- #
# Команда /admin — статистика
# --------------------------------------------------------------------------- #
@router.message(Command("admin", "stats"))
async def cmd_admin(message: Message, db: Database) -> None:
    """Показать статистику заявок и кнопку «📋 Активные брони»."""
    await message.answer(await _stats_text(db), reply_markup=admin_panel_keyboard())
    logger.info("Админ-панель: %s запросил статистику", message.from_user.id if message.from_user else "?")


@router.callback_query(F.data == CB_ADMIN_STATS)
async def cb_refresh_stats(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    """Кнопка «🔄 Обновить статистику»."""
    text = await _stats_text(db)
    await edit_or_send(
        bot,
        callback.message.chat.id,
        callback.message.message_id,
        text,
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
# Решения по заявкам: подтвердить / отклонить
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith((CB_ADMIN_APPROVE_PREFIX, CB_ADMIN_REJECT_PREFIX)))
async def cb_decision(callback: CallbackQuery, db: Database, bot: Bot, cfg: AppConfig) -> None:
    """Обработать нажатие «✅ Подтвердить» или «❌ Отклонить»."""
    parsed = parse_admin_decision(callback.data)
    if parsed is None:
        await callback.answer("Некорректная кнопка решения", show_alert=True)
        return
    status, booking_id = parsed
    approved = status == BookingStatus.APPROVED

    booking = await db.get_booking(booking_id)
    if booking is None:
        await callback.answer(f"Заявка №{booking_id} не найдена в базе", show_alert=True)
        return

    # Меняем статус только у ещё не обработанной заявки — защита от двойного клика.
    if not await db.set_status_if_pending(booking_id, status):
        fresh = await db.get_booking(booking_id) or booking
        await callback.answer(
            truncate(
                texts.ADMIN_ALREADY_PROCESSED.format(
                    booking_id=booking_id,
                    status=BookingStatus.title(fresh.status),
                ),
                200,
            ),
            show_alert=True,
        )
        await _apply_admin_view(
            bot,
            db,
            cfg,
            callback,
            texts.ADMIN_ALREADY_PROCESSED_NOTE.format(status=BookingStatus.title(fresh.status)),
        )
        return

    delivered = await deliver_decision(bot, booking, approved)

    decision_text = texts.ADMIN_DECISION_OK if approved else texts.ADMIN_DECISION_NO
    if not delivered:
        decision_text += " Клиенту уведомление не доставлено."
    await callback.answer(truncate(decision_text.format(booking_id=booking_id), 200))

    note = (texts.ADMIN_DECISION_APPROVED if approved else texts.ADMIN_DECISION_REJECTED).format(
        decided_at=now_full_text(cfg.timezone),
        notify_status=texts.CLIENT_NOTIFIED_OK if delivered else texts.CLIENT_NOTIFIED_FAIL,
    )
    await _apply_admin_view(bot, db, cfg, callback, note)

    logger.info(
        "Бронь №%s %s администратором %s, клиент уведомлён: %s",
        booking.id,
        "подтверждена" if approved else "отклонена",
        cfg.admin_id,
        delivered,
    )


__all__ = ["router"]
