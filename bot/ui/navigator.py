"""Навигатор: один экран — одно сообщение, которое бот перезаписывает.

Класс :class:`Navigator` решает главную задачу интерфейса: не плодить сообщения
в чате. Он помнит, какое сообщение является «экраном» пользователя, и всегда
перезаписывает именно его (``editMessageText``). Если Telegram не дал
отредактировать (сообщение старше 48 часов, удалено, текст не изменился) —
экран отправляется новым сообщением, а его ``message_id`` запоминается.

Кроме показа экранов навигатор ведёт **стек переходов**: «◀ Назад» возвращает
пользователя туда, откуда он пришёл, а не всегда в меню. Стек живёт в FSM-данных,
поэтому переживает перезапуск обработчика, но не перезапуск процесса.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.types import User as TelegramUser

from bot.config import AppConfig
from bot.db.database import Database
from bot.services.admins import AdminRegistry
from bot.states import DATA_SCREEN, DATA_SCREEN_MESSAGE, DATA_SCREEN_STACK
from bot.ui.render import MESSAGE_LIMIT, ViewContext, render_screen
from bot.ui.screens import SCREEN_BACK, Screen
from bot.utils.formatting import truncate
from bot.utils.telegram import safe_delete_message, safe_edit_message_text, send_screen

logger = logging.getLogger(__name__)

#: Глубина стека переходов: больше не нужно, чтобы «Назад» не вёл в бесконечность.
STACK_LIMIT: int = 20

#: Ограничение Telegram на длину текста всплывающего окна.
ALERT_LIMIT: int = 200

#: Ключи записи об экране в FSM-данных (JSON-совместимые — MemoryStorage и Redis).
_KEY_SCREEN = "s"
_KEY_ARG = "a"


def _entry(screen: str, arg: str = "") -> dict[str, str]:
    return {_KEY_SCREEN: screen, _KEY_ARG: arg}


class Navigator:
    """Показывает экраны и водит по ним пользователя.

    Один объект создаётся на один апдейт — из сообщения или из нажатия кнопки.
    Если навигатор создан из :class:`CallbackQuery`, он дополнительно умеет
    отвечать на сам запрос (:meth:`answer`) — иначе снять «часики» с кнопки
    было бы нечем.
    """

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
    ) -> None:
        self.bot = bot
        self.db = db
        self.cfg = cfg
        self.state = state
        self.admins = admins
        self.user_id = user_id
        self.user = user
        self.callback = callback
        #: id сообщения-экрана, если он известен сразу (нажатие кнопки).
        self.message_id = message_id
        #: Скрыть reply-клавиатуру при следующей отправке нового экрана.
        self._clear_reply_keyboard = False

    # --- конструкторы ------------------------------------------------------- #
    @classmethod
    def from_message(
        cls,
        message: Message,
        *,
        db: Database,
        cfg: AppConfig,
        state: FSMContext,
        admins: AdminRegistry | None = None,
    ) -> Navigator:
        """Навигатор для сообщения: id экрана достаём из FSM-памяти."""
        return cls(
            bot=message.bot,
            db=db,
            cfg=cfg,
            state=state,
            user_id=message.chat.id,
            admins=admins,
            user=message.from_user,
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
        """Навигатор для нажатия кнопки: экран — сообщение под кнопкой."""
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
        )

    # --- состояние ---------------------------------------------------------- #
    @property
    def context(self) -> ViewContext:
        """Контекст для рендереров экранов."""
        return ViewContext(
            db=self.db,
            cfg=self.cfg,
            bot=self.bot,
            user_id=self.user_id,
            state=self.state,
            admins=self.admins,
            user=self.user,
        )

    async def current(self) -> dict[str, str]:
        """Текущий экран (по умолчанию — меню)."""
        entry = (await self.state.get_data()).get(DATA_SCREEN)
        if isinstance(entry, dict) and entry.get(_KEY_SCREEN):
            return {
                _KEY_SCREEN: str(entry[_KEY_SCREEN]),
                _KEY_ARG: str(entry.get(_KEY_ARG) or ""),
            }
        return _entry(Screen.MENU)

    async def _stack(self) -> list[dict[str, str]]:
        stack = (await self.state.get_data()).get(DATA_SCREEN_STACK)
        if not isinstance(stack, list):
            return []
        return [
            item for item in stack if isinstance(item, dict) and item.get(_KEY_SCREEN)
        ]

    # --- переходы ----------------------------------------------------------- #
    async def show(
        self,
        screen: str,
        arg: str | int = "",
        *,
        push: bool = True,
        note: str | None = None,
    ) -> None:
        """Показать экран, перезаписав сообщение.

        ``push=True`` — запомнить текущий экран в стеке, чтобы «Назад» вернул на
        него. ``push=False`` — заменить экран без запоминания (обновление,
        возврат по стеку, сброс в меню).
        """
        if screen == SCREEN_BACK:
            await self.back()
            return

        arg = str(arg)
        previous = await self.current()
        same = (
            previous.get(_KEY_SCREEN) == screen
            and str(previous.get(_KEY_ARG) or "") == arg
        )

        stack = await self._stack()
        if push and not same and previous.get(_KEY_SCREEN):
            stack.append(previous)

        await self.state.update_data(
            **{
                DATA_SCREEN: _entry(screen, arg),
                DATA_SCREEN_STACK: stack[-STACK_LIMIT:],
            }
        )
        await self.redraw(note=note)

    async def back(self) -> None:
        """Вернуться на предыдущий экран; если стека нет — в меню."""
        stack = await self._stack()
        if not stack:
            await self.show(Screen.MENU, push=False)
            return
        previous = stack.pop()
        await self.state.update_data(**{DATA_SCREEN_STACK: stack})
        await self.show(
            str(previous.get(_KEY_SCREEN) or Screen.MENU),
            str(previous.get(_KEY_ARG) or ""),
            push=False,
        )

    async def refresh(self) -> None:
        """Перерисовать текущий экран теми же данными (кнопка «🔄 Обновить»)."""
        await self.redraw()

    async def reset(self, screen: str = Screen.MENU, arg: str = "") -> None:
        """Сбросить стек и показать экран — точка входа командой (/admin)."""
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
        """Начать новое сообщение-экран, старое удалить (``/start``).

        Так меню всегда оказывается внизу чата и видно сразу, а предыдущее
        рабочее сообщение бота не висит в истории мёртвым экраном. Заодно
        скрывается reply-клавиатура, если она осталась от старой версии бота.
        """
        self.message_id = None
        self._clear_reply_keyboard = True
        await self.state.update_data(
            **{
                DATA_SCREEN_STACK: [],
                DATA_SCREEN_MESSAGE: None,
                DATA_SCREEN: _entry(screen, arg),
            }
        )
        await self.show(screen, arg, push=False)
        if (
            old_message_id is not None
            and self.message_id is not None
            and int(old_message_id) != int(self.message_id)
        ):
            await safe_delete_message(self.bot, self.user_id, int(old_message_id))

    # --- отрисовка ---------------------------------------------------------- #
    async def redraw(self, *, note: str | None = None) -> None:
        """Перерисовать текущий экран в сообщении-экране."""
        entry = await self.current()
        text, markup = await render_screen(
            self.context, str(entry[_KEY_SCREEN]), str(entry.get(_KEY_ARG) or "")
        )
        if note:
            text = truncate(f"{note}\n\n{text}", MESSAGE_LIMIT)
        await self._draw(text, markup)

    async def _draw(self, text: str, markup: InlineKeyboardMarkup | None) -> None:
        """Отправить экран: правкой того же сообщения или новым сообщением."""
        message_id = self.message_id or (await self.state.get_data()).get(
            DATA_SCREEN_MESSAGE
        )

        if message_id is not None:
            if await safe_edit_message_text(
                self.bot, self.user_id, int(message_id), text, markup
            ):
                return
            logger.debug(
                "Экран %s/%s не отредактировался — отправляю новым сообщением",
                self.user_id,
                message_id,
            )

        new_id = await send_screen(
            self.bot,
            self.user_id,
            text,
            markup,
            clear_reply_keyboard=self._clear_reply_keyboard,
        )
        self._clear_reply_keyboard = False
        if new_id:
            self.message_id = new_id
            await self.state.update_data(**{DATA_SCREEN_MESSAGE: new_id})

    # --- ответы на нажатия -------------------------------------------------- #
    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        """Снять «часики» с кнопки: тост или всплывающее окно."""
        if self.callback is None:
            return
        if text:
            await self.callback.answer(truncate(text, ALERT_LIMIT), show_alert=alert)
        else:
            await self.callback.answer()

    async def alert(self, text: str) -> None:
        """Всплывающее окно с предупреждением."""
        await self.answer(text, alert=True)

    async def notify(
        self, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> None:
        """Отдельное сообщение вне экрана (например, результат подтверждения)."""
        await self.bot.send_message(
            chat_id=self.user_id,
            text=truncate(text, MESSAGE_LIMIT),
            reply_markup=markup,
        )

    def __repr__(self) -> str:  # pragma: no cover — отладочная мелочь
        return f"<Navigator user={self.user_id} message={self.message_id}>"


__all__ = ["ALERT_LIMIT", "STACK_LIMIT", "Navigator"]
