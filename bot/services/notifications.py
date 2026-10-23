"""Отправка уведомлений клиенту и администраторам клуба.

Любая отправка может упасть по сетевым причинам или потому, что пользователь
заблокировал бота (``TelegramForbiddenError``). Бот при этом не должен
останавливаться, а администратор — видеть честный статус доставки, поэтому все
вызовы обёрнуты в try/except.

Заявка рассылается **всем активным администраторам** (таблица ``admins``):
каждый получает свою копию фото чека с кнопками решения. Кто нажал первым — тот
и принял решение (статус в базе меняется атомарно), а у остальных кнопки гаснут
и появляется отметка о том, кто и что сделал (:func:`sync_peer_admins`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import Chat

from bot import texts
from bot.config import AppConfig
from bot.db.database import Database
from bot.db.models import Booking, BookingStatus, User
from bot.keyboards.inline import booking_decision_keyboard
from bot.utils.formatting import escape_html, format_datetime, now_full_text, truncate

logger = logging.getLogger(__name__)

#: Ограничения Telegram на длину подписи к медиа и текста сообщения.
CAPTION_LIMIT: int = 1024
MESSAGE_LIMIT: int = 4096
#: Ограничение на текст всплывающего ответа на нажатие кнопки.
CALLBACK_ANSWER_LIMIT: int = 200


@dataclass(slots=True)
class DeliveryReport:
    """Итог рассылки новой брони администраторам."""

    delivered: tuple[int, ...] = ()
    failed: tuple[int, ...] = ()
    #: Пары ``(admin_id, message_id)``. Сохраняются в ``booking_notifications``,
    #: чтобы после решения погасить кнопки у тех, кто не успел нажать.
    notifications: list[tuple[int, int]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Хотя бы один администратор получил заявку."""
        return bool(self.delivered)

    @property
    def total(self) -> int:
        """Сколько администраторов мы пытались уведомить."""
        return len(self.delivered) + len(self.failed)

    def summary(self) -> str:
        """Короткое описание для лога: «доставлено 2/3»."""
        return f"доставлено {len(self.delivered)}/{self.total}"


async def admin_recipients(db: Database, cfg: AppConfig) -> tuple[int, ...]:
    """Кому отправлять новую заявку: активные администраторы из базы.

    Если список пуст (свежая база до первой синхронизации, повреждённый файл),
    берём администраторов из ``.env`` — уведомления не должны пропадать молча.
    """
    try:
        ids = list(await db.active_admin_ids())
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось прочитать администраторов из базы")
        ids = []
    if not ids:
        ids = list(cfg.admin_ids)
        logger.warning("В базе нет активных администраторов — беру список из .env")
    return tuple(ids)


async def _send_booking_copy(
    bot: Bot,
    admin_id: int,
    booking: Booking,
    caption: str,
) -> int | None:
    """Отправить одну копию заявки администратору, вернуть ``message_id``."""
    try:
        if booking.receipt_file_id:
            message = await bot.send_photo(
                chat_id=admin_id,
                photo=booking.receipt_file_id,
                caption=truncate(caption, CAPTION_LIMIT),
                reply_markup=booking_decision_keyboard(booking.id),
            )
        else:
            message = await bot.send_message(
                chat_id=admin_id,
                text=truncate(caption, MESSAGE_LIMIT),
                reply_markup=booking_decision_keyboard(booking.id),
            )
    except TelegramForbiddenError:
        logger.error(
            "Администратор %s заблокировал бота или не начинал с ним диалог — "
            "заявка №%s ему не доставлена (пусть нажмёт /start)",
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
    """Разослать новую заявку всем администраторам: чек + кнопки решения.

    Возвращает :class:`DeliveryReport` — по нему вызывающий код сохраняет копии
    уведомлений в базу и сообщает клиенту, дошла ли заявка до админов.
    """
    recipients = await admin_recipients(db, cfg)
    if not recipients:
        logger.error(
            "Заявка №%s сохранена, но администраторы не заданы: проверьте "
            "ADMIN_IDS в .env или добавьте их через /admins",
            booking.id,
        )
        return DeliveryReport()

    client_link = user.link if user is not None else f"ID: <code>{booking.user_id}</code>"
    caption = texts.NEW_BOOKING_CAPTION.format(
        booking_id=booking.id,
        tariff=escape_html(booking.tariff),
        time_slot=escape_html(booking.time_slot),
        client_name=escape_html(booking.client_name),
        user_link=client_link,
        created_at=format_datetime(booking.created_at, cfg.timezone),
        admins_count=len(recipients),
    )

    delivered: list[int] = []
    failed: list[int] = []
    notifications: list[tuple[int, int]] = []

    for admin_id in recipients:
        message_id = await _send_booking_copy(bot, admin_id, booking, caption)
        if message_id is None:
            failed.append(admin_id)
            continue
        delivered.append(admin_id)
        notifications.append((admin_id, message_id))
        logger.info("Заявка №%s отправлена администратору %s", booking.id, admin_id)

    report = DeliveryReport(
        delivered=tuple(delivered),
        failed=tuple(failed),
        notifications=notifications,
    )
    if failed:
        logger.warning(
            "Заявка №%s: %s, не доставлено администраторам: %s",
            booking.id,
            report.summary(),
            ", ".join(str(i) for i in failed) or "—",
        )
    return report


async def resolve_decided_by(bot: Bot, db: Database, user_id: int | None) -> str:
    """Как подписать админа, принявшего решение: «@nickname», «Имя» или «ID …»."""
    if not user_id:
        return "неизвестно"

    stored = await db.get_user(int(user_id))
    if stored is not None and stored.mention:
        return stored.mention

    try:
        chat: Chat = await bot.get_chat(int(user_id))
    except TelegramAPIError:
        logger.debug("Не удалось получить профиль администратора %s", user_id)
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
    """Погасить кнопки у администраторов, которые не успели принять решение.

    :param exclude: ID администратора, принявшего решение, — его копию
        обрабатывает сам обработчик кнопки (отметкой о своём решении).
    :return: ID тех, кому отметку доставить не удалось (например, они так и
        не нажали /start в диалоге с ботом).
    """
    try:
        peers = await db.list_notifications(booking.id)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось прочитать копии уведомления заявки №%s", booking.id)
        return []
    if exclude is not None:
        peers = [(admin_id, message_id) for admin_id, message_id in peers if admin_id != exclude]
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
        except TelegramAPIError as exc:
            # Сообщение могло быть текстовым, удалённым или старше 48 часов.
            logger.debug(
                "Не удалось обновить копию заявки №%s у администратора %s: %s",
                booking.id,
                admin_id,
                exc,
            )
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=truncate(note, MESSAGE_LIMIT),
            )
        except TelegramAPIError as exc:
            failed.append(admin_id)
            logger.error(
                "Не удалось сообщить администратору %s о решении по заявке №%s: %s",
                admin_id,
                booking.id,
                exc,
            )

    logger.info(
        "Решение по заявке №%s разослано остальным администраторам: %d из %d",
        booking.id,
        len(peers) - len(failed),
        len(peers),
    )
    return failed


async def deliver_decision(bot: Bot, booking: Booking, approved: bool) -> bool:
    """Сообщить клиенту решение по его брони.

    Возвращает ``True``, если сообщение доставлено (``False`` — клиент
    заблокировал бота или возникла ошибка Telegram API).
    """
    if approved:
        text = texts.CLIENT_APPROVED.format(
            tariff=escape_html(booking.tariff),
            time_slot=escape_html(booking.time_slot),
            booking_id=booking.id,
            client_name=escape_html(booking.client_name),
        )
    else:
        text = texts.CLIENT_REJECTED.format(
            booking_id=booking.id,
            tariff=escape_html(booking.tariff),
            time_slot=escape_html(booking.time_slot),
        )

    try:
        await bot.send_message(chat_id=booking.user_id, text=truncate(text, MESSAGE_LIMIT))
    except TelegramForbiddenError:
        logger.warning(
            "Клиент %s заблокировал бота — решение по брони №%s не доставлено",
            booking.user_id,
            booking.id,
        )
        return False
    except TelegramAPIError:
        logger.exception(
            "Не удалось отправить клиенту %s решение по брони №%s",
            booking.user_id,
            booking.id,
        )
        return False

    logger.info(
        "Клиент %s уведомлён о решении по брони №%s: %s",
        booking.user_id,
        booking.id,
        "подтверждена" if approved else "отклонена",
    )
    return True


def decision_note(cfg: AppConfig, approved: bool, delivered: bool, decided_by: str) -> str:
    """Отметка под обработанной заявкой: что сделано, кем и дошёл ли клиент."""
    template = texts.ADMIN_DECISION_APPROVED if approved else texts.ADMIN_DECISION_REJECTED
    return template.format(
        decided_at=now_full_text(cfg.timezone),
        decided_by=escape_html(decided_by),
        notify_status=texts.CLIENT_NOTIFIED_OK if delivered else texts.CLIENT_NOTIFIED_FAIL,
    )


__all__ = [
    "CALLBACK_ANSWER_LIMIT",
    "CAPTION_LIMIT",
    "MESSAGE_LIMIT",
    "DeliveryReport",
    "admin_recipients",
    "decision_note",
    "deliver_decision",
    "deliver_new_booking",
    "resolve_decided_by",
    "sync_peer_admins",
]
