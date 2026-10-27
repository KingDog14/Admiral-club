"""Навигатор графического интерфейса: один фото-экран на весь диалог.

Класс :class:`Navigator` ведёт одно рабочее фото-сообщение пользователя:

* при первом открытии отправляет карточку через ``sendPhoto``;
* при всех нажатиях инлайн-кнопок (:class:`CallbackQuery`: меню, FAQ, правила,
  шаги брони, админ-панель) меняет карточку, подпись и кнопки **в том же
  сообщении** через ``editMessageMedia``;
* при входящих командах и сообщениях (:class:`Message`: ``/start``, ``/menu``,
  ``/cancel``, ``/privacy``, ``/help``, ``/admin``, фото чека и т.д.), которые
  появляются внизу чата, отправляет актуальную карточку **в самый низ чата**
  под сообщением пользователя и удаляет прежний экран бота сверху, чтобы
  интерфейс никогда не застревал наверху истории.
"""

from __future__ import annotations

import logging
import re

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.types import User as TelegramUser

from bot.config import AppConfig
from bot.db.database import Database
from bot.db.models import BookingStatus
from bot.services.admins import AdminRegistry
from bot.states import DATA_SCREEN, DATA_SCREEN_MESSAGE, DATA_SCREEN_STACK
from bot.ui.render import CAPTION_LIMIT, ScreenView, ViewContext, render_screen
from bot.ui.screens import (
    PARENT_PRESERVES_ARG,
    SCREEN_BACK,
    SCREEN_PARENTS,
    SCREEN_TITLES,
    Screen,
)
from bot.utils.formatting import truncate, truncate_html
from bot.utils.telegram import (
    safe_delete_message,
    safe_edit_message_media,
    send_photo_screen,
)

logger = logging.getLogger(__name__)

#: Максимальная глубина стека возвратов «◀ Назад».
STACK_LIMIT: int = 20

#: Ограничение Telegram на длину текста тоста/alert в callback.answer.
ALERT_LIMIT: int = 200
MESSAGE_TEXT_LIMIT: int = 4096

_KEY_SCREEN = "s"
_KEY_ARG = "a"


def _entry(screen: str, arg: str = "") -> dict[str, str]:
    return {_KEY_SCREEN: screen, _KEY_ARG: arg}


class Navigator:
    """Управляет показами графических карточек и стеком переходов «◀ Назад»."""

    def __init__(
        self,
        *,
        bot: Bot,
        db: Database,
        cfg: AppConfig,
        state: FSMContext,
        user_id: int,
        message_id: int | None = None,
        admins: AdminRegistry | None = None,
        user: TelegramUser | None = None,
        callback: CallbackQuery | None = None,
        relocate: bool = False,
        trigger_message_id: int | None = None,
    ) -> None:
        self.bot = bot
        self.db = db
        self.cfg = cfg
        self.state = state
        self.admins = admins
        self.user_id = user_id
        self.user = user
        self.callback = callback
        self.message_id = message_id
        self.relocate = relocate
        self.trigger_message_id = trigger_message_id

    @classmethod
    def from_message(
        cls,
        message: Message,
        *,
        db: Database,
        cfg: AppConfig,
        state: FSMContext,
        admins: AdminRegistry | None = None,
        relocate: bool = True,
    ) -> Navigator:
        return cls(
            bot=message.bot,
            db=db,
            cfg=cfg,
            state=state,
            user_id=message.chat.id,
            admins=admins,
            user=message.from_user,
            relocate=relocate,
            trigger_message_id=message.message_id,
        )

    @classmethod
    def from_callback(
        cls,
        callback: CallbackQuery,
        *,
        db: Database,
        cfg: AppConfig,
        state: FSMContext,
        admins: AdminRegistry | None = None,
    ) -> Navigator:
        message = callback.message
        return cls(
            bot=callback.bot,
            db=db,
            cfg=cfg,
            state=state,
            user_id=callback.from_user.id,
            message_id=message.message_id if message is not None else None,
            admins=admins,
            user=callback.from_user,
            callback=callback,
            relocate=False,
        )

    def on_input_deleted(self, deleted: bool) -> None:
        """Сообщить навигатору, удалена ли промежуточная реплика ввода.

        Если текстовый ответ пользователя (имя, своё время, ID администратора)
        удалён ботом и между экраном и ответом не появлялось других сообщений,
        карточка плавно обновляется на месте. Иначе экран переносится вниз чата.
        """
        self.relocate = not deleted

    @property
    def context(self) -> ViewContext:
        return ViewContext(
            db=self.db,
            cfg=self.cfg,
            bot=self.bot,
            user_id=self.user_id,
            state=self.state,
            admins=self.admins,
            user=self.user,
        )

    def _infer_from_callback_message(self) -> dict[str, str] | None:
        """Восстановить текущий экран по заголовку сообщения (после перезапуска RAM)."""
        if self.callback is None or self.callback.message is None:
            return None
        raw = self.callback.message.caption or self.callback.message.text or ""
        if not raw:
            return None
        first_line = raw.splitlines()[0]
        # В FAQ и FAQ_ITEM заголовок одинаковый — различаем по приглашению списка.
        if SCREEN_TITLES[Screen.FAQ] in first_line:
            if "Выберите вопрос" in raw:
                return _entry(Screen.FAQ)
            return _entry(Screen.FAQ_ITEM)
        for scr, title in SCREEN_TITLES.items():
            if scr in (Screen.FAQ, Screen.FAQ_ITEM):
                continue
            if title and title in first_line:
                num_match = re.search(r"№(\d+)", raw)
                inferred_arg = num_match.group(1) if num_match else ""
                return _entry(scr, inferred_arg)
        return None

    async def current(self) -> dict[str, str]:
        entry = (await self.state.get_data()).get(DATA_SCREEN)
        if isinstance(entry, dict) and entry.get(_KEY_SCREEN):
            return {
                _KEY_SCREEN: str(entry[_KEY_SCREEN]),
                _KEY_ARG: str(entry.get(_KEY_ARG) or ""),
            }
        inferred = self._infer_from_callback_message()
        if inferred is not None:
            return inferred
        return _entry(Screen.MENU)

    async def resolve_parent(self, screen: str, arg: str = "") -> tuple[str, str]:
        """Определить родительский экран в глобальной иерархии интерфейса."""
        if screen == Screen.ADMIN_BOOKING:
            try:
                booking_id = int(arg)
            except (TypeError, ValueError):
                return Screen.ADMIN, ""
            booking = await self.db.get_booking(booking_id)
            if booking is None:
                return Screen.ADMIN, ""
            if booking.status == BookingStatus.APPROVED:
                return Screen.ADMIN_APPROVED, "0"
            if booking.status == BookingStatus.REJECTED:
                return Screen.ADMIN_REJECTED, "0"
            return Screen.ADMIN_PENDING, "0"

        parent = SCREEN_PARENTS.get(screen, Screen.MENU)
        parent_arg = arg if screen in PARENT_PRESERVES_ARG else ""
        return parent, parent_arg

    # --- переходы ----------------------------------------------------------- #
    async def show(
        self,
        screen: str,
        arg: str | int = "",
        *,
        push: bool = True,
        note: str | None = None,
    ) -> None:
        """Показать экран, обновив карточку и подпись текущего сообщения."""
        if screen == SCREEN_BACK:
            await self.back()
            return

        arg = str(arg)
        await self.state.update_data(
            **{
                DATA_SCREEN: _entry(screen, arg),
                DATA_SCREEN_STACK: [],
            }
        )
        await self.redraw(note=note)

    async def back(self) -> None:
        """Подняться на один уровень вверх в глобальной иерархии интерфейса."""
        cur = await self.current()
        parent_screen, parent_arg = await self.resolve_parent(
            cur.get(_KEY_SCREEN) or Screen.MENU,
            cur.get(_KEY_ARG) or "",
        )
        await self.show(parent_screen, parent_arg, push=False)

    async def refresh(self) -> None:
        """Перерисовать текущий экран свежими данными."""
        await self.redraw()

    async def reset(self, screen: str = Screen.MENU, arg: str = "") -> None:
        """Сбросить стек переходов и показать выбранный экран."""
        await self.state.update_data(
            **{DATA_SCREEN_STACK: [], DATA_SCREEN: _entry(screen, arg)}
        )
        await self.show(screen, arg, push=False)

    async def restart(
        self,
        screen: str = Screen.MENU,
        arg: str = "",
        *,
        old_message_id: int | None = None,
    ) -> None:
        """Отправить свежую карточку в низ чата, а старый экран убрать (``/start``)."""
        data = await self.state.get_data()
        previous_id = (
            old_message_id
            if old_message_id is not None
            else data.get(DATA_SCREEN_MESSAGE)
        )
        self.message_id = None
        self.relocate = True
        await self.state.update_data(
            **{
                DATA_SCREEN_STACK: [],
                DATA_SCREEN_MESSAGE: previous_id,
                DATA_SCREEN: _entry(screen, arg),
            }
        )
        await self.show(screen, arg, push=False)

    # --- отрисовка ---------------------------------------------------------- #
    async def redraw(self, *, note: str | None = None) -> None:
        """Перерисовать текущий экран в фото-сообщении."""
        entry = await self.current()
        view = await render_screen(
            self.context, str(entry[_KEY_SCREEN]), str(entry.get(_KEY_ARG) or "")
        )
        caption = view.caption
        if note:
            wrapped = (
                note if "<blockquote>" in note else f"<blockquote>{note}</blockquote>"
            )
            caption = truncate_html(f"{wrapped}\n\n{caption}", CAPTION_LIMIT)
        await self._draw(view, caption)

    async def _draw(self, view: ScreenView, caption: str) -> None:
        """Обновить медиа-карточку на месте или перенести экран в самый низ чата."""
        stored_id = (await self.state.get_data()).get(DATA_SCREEN_MESSAGE)
        message_id = self.message_id or stored_id
        has_intervening_messages = (
            stored_id is not None
            and self.trigger_message_id is not None
            and int(self.trigger_message_id) > int(stored_id) + 1
        )
        should_relocate = self.relocate or has_intervening_messages

        if message_id is not None and not should_relocate:
            mid = int(message_id)
            if await safe_edit_message_media(
                self.bot,
                self.user_id,
                mid,
                view.card,
                caption,
                view.reply_markup,
            ):
                self.message_id = mid
                if stored_id != mid:
                    await self.state.update_data(**{DATA_SCREEN_MESSAGE: mid})
                return
            logger.debug(
                "Медиа-экран %s/%s не обновился — отправляю новую карточку",
                self.user_id,
                mid,
            )

        new_id = await send_photo_screen(
            self.bot,
            self.user_id,
            view.card,
            caption,
            view.reply_markup,
        )
        if new_id:
            if message_id is not None and int(message_id) != int(new_id):
                await safe_delete_message(self.bot, self.user_id, int(message_id))
            self.message_id = new_id
            self.relocate = False
            self.trigger_message_id = None
            await self.state.update_data(**{DATA_SCREEN_MESSAGE: new_id})

    # --- ответы на callback ------------------------------------------------- #
    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        if self.callback is None:
            return
        if text:
            await self.callback.answer(truncate(text, ALERT_LIMIT), show_alert=alert)
        else:
            await self.callback.answer()

    async def alert(self, text: str) -> None:
        await self.answer(text, alert=True)

    async def notify(
        self, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> None:
        await self.bot.send_message(
            chat_id=self.user_id,
            text=truncate(text, MESSAGE_TEXT_LIMIT),
            reply_markup=markup,
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Navigator user={self.user_id} message={self.message_id}>"


__all__ = ["ALERT_LIMIT", "STACK_LIMIT", "Navigator"]
