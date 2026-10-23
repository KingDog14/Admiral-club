#!/usr/bin/env python3
"""Проверка бота без запуска Telegram: база, логика, клавиатуры, весь сценарий.

Запуск::

    python tests/smoke_test.py

Тест не требует интернета и настоящего токена. Он:

* поднимает временную базу SQLite и проверяет все запросы репозитория;
* использует **настоящие** объекты aiogram (``Message``, ``CallbackQuery``,
  ``FSMContext`` на ``MemoryStorage``) и подставного бота :class:`BotSpy`,
  который записывает все исходящие вызовы — так проверяется реальный путь
  ``await message.answer(...)`` / ``bot.edit_message_text(...)``;
* прогоняет сценарий целиком: /start → согласие (152-ФЗ) → тариф → время →
  имя → фото чека → уведомление админа → подтверждение → уведомление клиента
  → повторное нажатие кнопки → отклонение другой заявки → /admin;
* проверяет сборку роутеров: порядок подключения, фильтры админа/приватного
  чата, middleware согласия, длину и уникальность ``callback_data``,
  рендер всех текстовых шаблонов.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.exceptions import TelegramBadRequest  # noqa: E402
from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.methods import EditMessageText  # noqa: E402
from aiogram.types import (  # noqa: E402
    CallbackQuery,
    Chat,
    Message,
    PhotoSize,
)
from aiogram.types import (
    User as TelegramUser,
)

from bot import texts  # noqa: E402
from bot.config import AppConfig, ConfigError, load_config  # noqa: E402
from bot.db.database import Database, init_database  # noqa: E402
from bot.db.models import AdminRole, BookingStatus  # noqa: E402
from bot.filters.access import IsAdmin, IsPrivateChat  # noqa: E402
from bot.handlers import admin as admin_handlers  # noqa: E402
from bot.handlers import client as client_handlers  # noqa: E402
from bot.keyboards import inline as inline_keyboards  # noqa: E402
from bot.keyboards import reply as reply_keyboards  # noqa: E402
from bot.middlewares.consent import ConsentMiddleware  # noqa: E402
from bot.services.admins import AdminRegistry  # noqa: E402
from bot.services.booking import (  # noqa: E402
    clean_client_name,
    clean_custom_time,
    is_menu_button_text,
    time_slot_text,
)
from bot.services.notifications import (  # noqa: E402
    DeliveryReport,
    admin_recipients,
    deliver_new_booking,
    sync_peer_admins,
)
from bot.states import DATA_TARIFF_KEY, DATA_TIME_SLOT, AdminFSM, BookingFSM  # noqa: E402

TEST_TOKEN = "1234567890:AAH_test_token_for_local_checks_only"
DB_CLIENT_ID = 111_222_333  # клиент из блока проверок базы данных
SCENARIO_CLIENT_ID = 555_666_777  # клиент сквозного сценария (без согласия в базе)
CANCEL_CLIENT_ID = 777_000_111  # клиент для проверки отмены
ADMIN_ID = 999_888_777  # владелец бота (первый ADMIN_IDS)
ADMIN_ID_2 = 888_777_666  # второй администратор из .env
ADMIN_ID_3 = 123_456_789  # администратор, добавленный через панель
MULTI_CLIENT_ID = 321_654_987  # клиент для проверки рассылки нескольким админам
TEST_TZ = ZoneInfo("Asia/Sakhalin")

_results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, details: str = "") -> None:
    """Записать результат проверки и напечатать его."""
    _results.append((name, bool(condition), details))
    mark = "✅" if condition else "❌"
    line = f"{mark} {name}"
    if details and not condition:
        line += f" — {details}"
    print(line)


def check_equal(name: str, actual: Any, expected: Any) -> None:
    check(name, actual == expected, f"ожидалось {expected!r}, получено {actual!r}")


# --------------------------------------------------------------------------- #
# Подставной бот: записывает все исходящие вызовы
# --------------------------------------------------------------------------- #
class BotSpy:
    """«Бот», который ничего не отправляет, а складывает вызовы в :attr:`calls`.

    Принимаются оба пути отправки, которые использует бот:

    * ``await message.answer(...)`` → aiogram вызывает ``bot(method)``;
    * ``await bot.send_message(...)`` → прямой вызов метода.

    Все записи приводятся к ``(имя метода, SimpleNamespace с параметрами)``,
    чтобы проверки читались одинаково для обоих путей.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.id = 1  # нужен FSMContextMiddleware (StorageKey.bot_id) и логу feed_update
        self.token = TEST_TOKEN
        self.default = DefaultBotProperties(parse_mode=ParseMode.HTML)
        self.session = SimpleNamespace(close=_noop)
        self._message_ids = 1000  # «Telegram» выдаёт растущие id сообщений
        #: chat_id → профиль, который вернёт get_chat (для подписи «решение принял …»)
        self.profiles: dict[int, SimpleNamespace] = {}

    def _sent_message(self, kwargs: dict[str, Any]) -> SimpleNamespace:
        """Подставить результат отправки: бот использует ``message.message_id``."""
        self._message_ids += 1
        return SimpleNamespace(
            message_id=self._message_ids,
            chat=SimpleNamespace(id=kwargs.get("chat_id")),
        )

    # --- путь через TelegramMethod (message.answer / callback.answer) ------- #
    async def __call__(self, method: Any, timeout: int | None = None) -> None:
        self.calls.append((type(method).__name__, method))

    # --- прямые вызовы bot.<method>(...) ----------------------------------- #
    async def send_message(self, **kwargs: Any) -> Any:
        self.calls.append(("SendMessage", SimpleNamespace(**kwargs)))
        return self._sent_message(kwargs)

    async def send_photo(self, **kwargs: Any) -> Any:
        self.calls.append(("SendPhoto", SimpleNamespace(**kwargs)))
        return self._sent_message(kwargs)

    async def get_chat(self, chat_id: int, **kwargs: Any) -> Any:
        """Профиль пользователя — нужен, если администратора нет в таблице users."""
        self.calls.append(("GetChat", SimpleNamespace(chat_id=chat_id, **kwargs)))
        if chat_id in self.profiles:
            return self.profiles[chat_id]
        return SimpleNamespace(id=chat_id, username=None, full_name=f"Пользователь {chat_id}")

    async def send_contact(self, **kwargs: Any) -> None:
        self.calls.append(("SendContact", SimpleNamespace(**kwargs)))

    async def edit_message_text(self, **kwargs: Any) -> None:
        self.calls.append(("EditMessageText", SimpleNamespace(**kwargs)))

    async def edit_message_caption(self, **kwargs: Any) -> None:
        self.calls.append(("EditMessageCaption", SimpleNamespace(**kwargs)))

    async def delete_message(self, **kwargs: Any) -> None:
        self.calls.append(("DeleteMessage", SimpleNamespace(**kwargs)))

    # --- вспомогательные выборки ------------------------------------------- #
    def select(self, *method_names: str) -> list[Any]:
        return [payload for name, payload in self.calls if name in method_names]

    def messages(self) -> list[Any]:
        """Все исходящие тексты/подписи: отправка сообщений и фото."""
        return self.select("SendMessage", "SendPhoto")

    def texts(self) -> list[str]:
        return [text_of(payload) for payload in self.messages()]

    def answers(self) -> list[Any]:
        """Ответы на callback-запросы (тосты и alert'ы)."""
        return self.select("AnswerCallbackQuery")

    def mark(self) -> int:
        """Метка «до»: всё, что добавится после неё, — новые вызовы."""
        return len(self.calls)

    def since(self, mark: int) -> list[tuple[str, Any]]:
        return self.calls[mark:]

    def new_messages(self, mark: int) -> list[Any]:
        return [
            payload
            for name, payload in self.since(mark)
            if name in ("SendMessage", "SendPhoto")
        ]

    def new_texts(self, mark: int) -> list[str]:
        return [text_of(payload) for payload in self.new_messages(mark)]

    def step_payloads(self, mark: int) -> list[Any]:
        """Все исходящие вызовы шага: новые сообщения и правки существующих."""
        return [
            payload
            for name, payload in self.since(mark)
            if name in ("SendMessage", "SendPhoto", "EditMessageText", "EditMessageCaption")
        ]

    def step_texts(self, mark: int) -> list[str]:
        """Все тексты шага: и новые сообщения, и правки существующих.

        Сценарий бронирования обновляет одно и то же сообщение (EditMessageText),
        поэтому для проверки «что увидел клиент» нужно учитывать оба пути.
        """
        return [text_of(payload) for payload in self.step_payloads(mark)]


async def _noop() -> None:
    return None


def text_of(payload: Any) -> str:
    """Текст сообщения или подпись к фото из записанного вызова."""
    return str(getattr(payload, "text", None) or getattr(payload, "caption", None) or "")


def markup_of(payload: Any) -> Any:
    return getattr(payload, "reply_markup", None)


# --------------------------------------------------------------------------- #
# Фабрики настоящих объектов aiogram
# --------------------------------------------------------------------------- #
def make_message(
    *,
    user_id: int,
    bot: BotSpy,
    text: str | None = None,
    with_photo: bool = False,
    caption: str | None = None,
    username: str | None = "client",
    first_name: str = "Клиент",
    message_id: int = 1,
) -> Message:
    """Собрать валидный :class:`aiogram.types.Message` и привязать к «боту»."""
    photo = None
    if with_photo:
        # Telegram присылает три размера; хендлер обязан взять самый большой.
        photo = [
            PhotoSize(file_id=f"AgAC_size_{width}", file_unique_id=f"u{width}", width=width, height=width)
            for width in (90, 320, 1280)
        ]
    return Message(
        message_id=message_id,
        date=datetime.now(TEST_TZ),
        chat=Chat(id=user_id, type="private"),
        from_user=TelegramUser(
            id=user_id, is_bot=False, first_name=first_name, username=username
        ),
        text=text,
        caption=caption,
        photo=photo,
    ).as_(bot)


def make_callback(
    data: str,
    *,
    user_id: int,
    bot: BotSpy,
    with_photo: bool = False,
    caption: str = "",
) -> CallbackQuery:
    """Собрать валидный :class:`aiogram.types.CallbackQuery` под кнопкой."""
    source = make_message(
        user_id=user_id,
        bot=bot,
        with_photo=with_photo,
        caption=caption,
        username="admin" if user_id == ADMIN_ID else "client",
        first_name="Админ" if user_id == ADMIN_ID else "Клиент",
        message_id=42,
    )
    return CallbackQuery(
        id=f"cb_{abs(hash(data)) % 10**8}",
        from_user=source.from_user,
        chat_instance="test-instance",
        message=source,
        data=data,
    ).as_(bot)


def make_fsm_context(user_id: int) -> FSMContext:
    """Настоящий FSMContext на MemoryStorage — как в работающем боте."""
    storage = MemoryStorage()
    key = StorageKey(user_id=user_id, chat_id=user_id, bot_id=1)
    return FSMContext(storage=storage, key=key)


def make_cfg(db_path: Path, admin_ids: tuple[int, ...] = (ADMIN_ID,)) -> AppConfig:
    """Конфигурация теста: по умолчанию один администратор (как в старой версии)."""
    return AppConfig(
        bot_token=TEST_TOKEN,
        admin_ids=admin_ids,
        db_path=db_path,
        timezone=TEST_TZ,
        log_level="ERROR",
    )


# --------------------------------------------------------------------------- #
# 1. Конфигурация
# --------------------------------------------------------------------------- #
ENV_KEYS = ("BOT_TOKEN", "ADMIN_ID", "ADMIN_IDS", "DB_PATH", "TZ", "LOG_LEVEL")


def _clear_env() -> None:
    """Очистить окружение: load_dotenv не перезаписывает уже заданные переменные."""
    for key in ENV_KEYS:
        os.environ.pop(key, None)


def test_config() -> None:
    print("\n— Конфигурация —")
    env_dir = Path(tempfile.mkdtemp())
    try:
        _clear_env()
        empty_env = env_dir / "empty.env"
        empty_env.write_text("\n", encoding="utf-8")
        try:
            load_config(empty_env)
            check("Пустой .env должен вызывать ConfigError", False, "исключение не поднято")
        except ConfigError as exc:
            check("Пустой .env → понятная ошибка про BOT_TOKEN", "BOT_TOKEN" in str(exc))

        _clear_env()
        good_env = env_dir / "good.env"
        good_env.write_text(
            f"BOT_TOKEN={TEST_TOKEN}\nADMIN_ID={ADMIN_ID}\nTZ=Europe/Moscow\n",
            encoding="utf-8",
        )
        cfg = load_config(good_env)
        check_equal("BOT_TOKEN прочитан", cfg.bot_token, TEST_TOKEN)
        check_equal("ADMIN_ID прочитан как число", cfg.admin_id, ADMIN_ID)
        check_equal("Часовой пояс из .env применён", str(cfg.timezone), "Europe/Moscow")
        check("Путь к базе по умолчанию — внутри проекта", cfg.db_path.name == "admiral.db")

        _clear_env()
        bad_admin = env_dir / "bad.env"
        bad_admin.write_text(f"BOT_TOKEN={TEST_TOKEN}\nADMIN_ID=@nickname\n", encoding="utf-8")
        try:
            load_config(bad_admin)
            check("ADMIN_ID=@nickname должен отклоняться", False, "исключение не поднято")
        except ConfigError as exc:
            check("ADMIN_ID не число → понятная ошибка", "числовой" in str(exc))

        # --- несколько администраторов через ADMIN_IDS ----------------------- #
        _clear_env()
        multi_env = env_dir / "multi.env"
        multi_env.write_text(
            f"BOT_TOKEN={TEST_TOKEN}\nADMIN_IDS={ADMIN_ID}, {ADMIN_ID_2};{ADMIN_ID_3}\n",
            encoding="utf-8",
        )
        cfg_multi = load_config(multi_env)
        check_equal(
            "ADMIN_IDS читается списком (запятая, пробел, точка с запятой)",
            cfg_multi.admin_ids,
            (ADMIN_ID, ADMIN_ID_2, ADMIN_ID_3),
        )
        check_equal("Владелец — первый ID из ADMIN_IDS", cfg_multi.admin_id, ADMIN_ID)
        check("Первый ID распознаётся как администратор", cfg_multi.is_env_admin(ADMIN_ID))
        check("Второй ID распознаётся как администратор", cfg_multi.is_env_admin(ADMIN_ID_2))
        check("Посторонний ID не администратор", not cfg_multi.is_env_admin(DB_CLIENT_ID))

        _clear_env()
        dedup_env = env_dir / "dedup.env"
        dedup_env.write_text(
            f"BOT_TOKEN={TEST_TOKEN}\nADMIN_IDS={ADMIN_ID},{ADMIN_ID},{ADMIN_ID_2}\n",
            encoding="utf-8",
        )
        check_equal(
            "Повторы в ADMIN_IDS убираются, порядок сохраняется",
            load_config(dedup_env).admin_ids,
            (ADMIN_ID, ADMIN_ID_2),
        )

        _clear_env()
        os.environ["BOT_TOKEN"] = TEST_TOKEN
        os.environ["ADMIN_IDS"] = f"{ADMIN_ID_2},{ADMIN_ID}"
        os.environ["ADMIN_ID"] = str(ADMIN_ID)
        check_equal(
            "ADMIN_IDS важнее устаревшего ADMIN_ID",
            load_config(None).admin_ids,
            (ADMIN_ID_2, ADMIN_ID),
        )

        _clear_env()
        bad_multi = env_dir / "bad_multi.env"
        bad_multi.write_text(
            f"BOT_TOKEN={TEST_TOKEN}\nADMIN_IDS={ADMIN_ID},@nickname\n", encoding="utf-8"
        )
        try:
            load_config(bad_multi)
            check("ADMIN_IDS с @nickname должен отклоняться", False, "исключение не поднято")
        except ConfigError as exc:
            check(
                "ADMIN_IDS: понятная ошибка про числовой ID",
                "числовой" in str(exc) and "ADMIN_IDS" in str(exc),
            )

        _clear_env()
        no_admins = env_dir / "no_admins.env"
        no_admins.write_text(f"BOT_TOKEN={TEST_TOKEN}\n", encoding="utf-8")
        try:
            load_config(no_admins)
            check("Без ADMIN_IDS запуск должен отклоняться", False, "исключение не поднято")
        except ConfigError as exc:
            check("Без ADMIN_IDS ошибка объясняет, что вписать", "ADMIN_IDS" in str(exc))

        _clear_env()
        bad_tz = env_dir / "bad_tz.env"
        bad_tz.write_text(f"BOT_TOKEN={TEST_TOKEN}\nADMIN_ID={ADMIN_ID}\nTZ=Mars/Olympus\n", encoding="utf-8")
        try:
            load_config(bad_tz)
            check("Неизвестный часовой пояс должен отклоняться", False, "исключение не поднято")
        except ConfigError as exc:
            check("Неизвестный TZ → понятная ошибка", "часовой пояс" in str(exc))
    finally:
        _clear_env()
        shutil.rmtree(env_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 2. База данных
# --------------------------------------------------------------------------- #
async def test_database(db: Database) -> None:
    print("\n— База данных (SQLite) —")

    tables = await db.list_tables()
    check("Схема создана: users и bookings", {"users", "bookings"}.issubset(set(tables)), str(tables))

    user = await db.upsert_user(DB_CLIENT_ID, "ivan_petrov", "Иван")
    check_equal("Пользователь создан", user.user_id, DB_CLIENT_ID)
    check("Согласие по умолчанию не дано (152-ФЗ)", user.consent_given is False)
    check("has_consent = False до нажатия кнопки", (await db.has_consent(DB_CLIENT_ID)) is False)

    await db.set_consent(DB_CLIENT_ID, granted=True)
    check("Согласие зафиксировано", (await db.has_consent(DB_CLIENT_ID)) is True)

    await db.upsert_user(DB_CLIENT_ID, "ivan_new", "Иван Петров")
    refreshed = await db.get_user(DB_CLIENT_ID)
    check("Профиль обновляется", refreshed.username == "ivan_new" and refreshed.first_name == "Иван Петров")
    check("Согласие не теряется при обновлении профиля", refreshed.consent_given is True)

    booking = await db.add_booking(
        user_id=DB_CLIENT_ID,
        tariff="S VIP",
        time_slot="На ночь (22:00–08:00)",
        client_name="Иван",
        receipt_file_id="AgAC_biggest_photo",
    )
    check("Заявка создана с id", booking.id > 0)
    check_equal("Статус новой заявки — pending", booking.status, BookingStatus.PENDING)
    check("file_id чека сохранён", booking.receipt_file_id == "AgAC_biggest_photo")
    check("created_at заполнен", bool(booking.created_at))

    first = await db.set_status_if_pending(booking.id, BookingStatus.APPROVED)
    second = await db.set_status_if_pending(booking.id, BookingStatus.REJECTED)
    check("Первое решение применено", first is True)
    check("Повторное решение отклонено (защита от двойного клика)", second is False)
    check_equal(
        "Статус в базе остался approved",
        (await db.get_booking(booking.id)).status,
        BookingStatus.APPROVED,
    )

    for index in range(7):
        await db.add_booking(
            user_id=DB_CLIENT_ID,
            tariff="VIP",
            time_slot="Сейчас (с 20:00, 23.10.2026)",
            client_name=f"Гость {index}",
            receipt_file_id=f"AgAC_{index}",
        )
    check_equal("Подсчёт pending-заявок", await db.count_bookings(BookingStatus.PENDING), 7)

    page = await db.list_bookings_with_users(
        BookingStatus.PENDING, limit=5, offset=0, oldest_first=True
    )
    check_equal("Размер страницы списка активных броней", len(page), 5)
    check("JOIN с users возвращает профиль клиента", page[0][1] is not None)
    check("Заявки в списке идут по возрастанию id", page[0][0].id < page[1][0].id)

    stats = await db.get_stats()
    check_equal("Статистика: всего заявок", stats.total, 8)
    check_equal("Статистика: ожидают", stats.pending, 7)
    check_equal("Статистика: подтверждено", stats.approved, 1)
    check_equal("Статистика: пользователей", stats.users, 1)
    check_equal("Статистика: дали согласие", stats.consented, 1)
    check("as_dict() пригоден для format(**stats)", set(stats.as_dict()) >= {"total", "pending"})

    history = await db.list_user_bookings(DB_CLIENT_ID, limit=3)
    check_equal("История заявок клиента ограничена limit", len(history), 3)

    created = await asyncio.gather(
        *(
            db.add_booking(DB_CLIENT_ID, "Standart", "Сейчас", f"Поток {i}", f"AgAC_c{i}")
            for i in range(5)
        )
    )
    check("5 параллельных записей прошли без блокировки", len({b.id for b in created}) == 5)

    try:
        await db.add_booking(DB_CLIENT_ID, "VIP", "Сейчас", "Тест", "x", status="unknown")
        check("Недопустимый статус должен отклоняться", False, "исключение не поднято")
    except ValueError:
        check("Недопустимый статус брони отклоняется", True)


# --------------------------------------------------------------------------- #
# 3. Логика бронирования
# --------------------------------------------------------------------------- #
def test_booking_logic() -> None:
    print("\n— Логика бронирования —")
    moment = datetime(2026, 10, 23, 17, 51, tzinfo=TEST_TZ)

    check_equal("«Сейчас» фиксирует время обращения", time_slot_text("now", moment), "Сейчас (с 17:51, 23.10.2026)")
    check_equal("«На ночь» → ночной пакет", time_slot_text("night", moment), "На ночь (22:00–08:00)")
    check("«Другое» требует ввода текстом", time_slot_text("other", moment) is None)

    check_equal("Имя приводится к аккуратному виду", clean_client_name("  иван  "), "Иван")
    check_equal("Имя с дефисом проходит", clean_client_name("анна-мария"), "Анна-мария")
    check("Имя из одной буквы отклоняется", clean_client_name("И") is None)
    check("Имя длиннее 40 символов отклоняется", clean_client_name("И" * 41) is None)
    check("Команда вместо имени отклоняется", clean_client_name("/start") is None)
    check("Кнопка меню вместо имени отклоняется", clean_client_name("🎮 Забронировать") is None)
    check("Ссылка вместо имени отклоняется", clean_client_name("http://spam.ru") is None)
    check("Имя с цифрами отклоняется", clean_client_name("Иван2") is None)

    check_equal(
        "Своё время сохраняется текстом",
        clean_custom_time(" сегодня в 18:30 на 3 часа "),
        "сегодня в 18:30 на 3 часа",
    )
    check("Время без цифр отклоняется", clean_custom_time("когда-нибудь") is None)
    check("Время длиннее 60 символов отклоняется", clean_custom_time("в 18:00 " + "а" * 60) is None)
    check("Команда вместо времени отклоняется", clean_custom_time("/cancel") is None)

    check("Нажатие кнопки меню распознаётся", is_menu_button_text(reply_keyboards.MENU_PRICE) is True)
    check("Обычный текст кнопкой меню не считается", is_menu_button_text("Иван") is False)


# --------------------------------------------------------------------------- #
# 4. Клавиатуры и callback_data
# --------------------------------------------------------------------------- #
def test_keyboards() -> None:
    print("\n— Клавиатуры —")
    menu = reply_keyboards.main_menu()
    buttons = [button.text for row in menu.keyboard for button in row]
    check_equal("Главное меню: 4 кнопки", len(buttons), 4)
    check_equal(
        "Состав главного меню",
        buttons,
        [
            reply_keyboards.MENU_PRICE,
            reply_keyboards.MENU_RULES,
            reply_keyboards.MENU_BOOKING,
            reply_keyboards.MENU_CONTACTS,
        ],
    )
    check("Меню уменьшенное (resize_keyboard)", menu.resize_keyboard is True)

    consent = inline_keyboards.consent_keyboard()
    check_equal("Экран согласия: одна кнопка", len(consent.inline_keyboard[0]), 1)
    check_equal("Текст кнопки согласия", consent.inline_keyboard[0][0].text, "✅ Я согласен")

    tariffs = inline_keyboards.tariffs_keyboard()
    tariff_buttons = [b for row in tariffs.inline_keyboard for b in row]
    check_equal("Шаг 1: 4 тарифа + отмена", len(tariff_buttons), 5)
    check_equal(
        "Тарифы: Standart, VIP, S VIP, Double",
        [texts.TARIFF_TITLE_BY_KEY[key] for key in texts.TARIFF_TITLE_BY_KEY],
        ["Standart", "VIP", "S VIP", "Double"],
    )

    times = inline_keyboards.time_slots_keyboard()
    check_equal("Шаг 2: 3 варианта времени + отмена", len([b for row in times.inline_keyboard for b in row]), 4)

    decision = inline_keyboards.booking_decision_keyboard(123)
    check_equal("Под чеком две кнопки", len(decision.inline_keyboard[0]), 2)
    check_equal("Текст кнопки подтверждения", decision.inline_keyboard[0][0].text, "✅ Подтвердить")
    check_equal("Текст кнопки отклонения", decision.inline_keyboard[0][1].text, "❌ Отклонить")

    pending = inline_keyboards.pending_list_keyboard(
        [SimpleNamespace(id=1), SimpleNamespace(id=2)], page=1, pages=3
    )
    check("Навигация по страницам: назад, вперёд, статистика", len(pending.inline_keyboard[-1]) == 3)

    named_keyboards = {
        "согласие": consent,
        "тарифы": tariffs,
        "время": times,
        "решение по брони": decision,
        "админ-панель": inline_keyboards.admin_panel_keyboard(),
        "список заявок": pending,
    }
    all_callbacks = [
        b.callback_data
        for kb in named_keyboards.values()
        for row in kb.inline_keyboard
        for b in row
        if b.callback_data
    ]
    check(
        "Все callback_data ≤ 64 байт (лимит Telegram)",
        all(len(data.encode()) <= 64 for data in all_callbacks),
        str([d for d in all_callbacks if len(d.encode()) > 64]),
    )
    for kb_name, kb in named_keyboards.items():
        kb_callbacks = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
        check(
            f"Клавиатура «{kb_name}»: callback_data уникальны",
            len(set(kb_callbacks)) == len(kb_callbacks),
            str(kb_callbacks),
        )

    check_equal("Разбор тарифа из callback", inline_keyboards.parse_tariff_key("bk:t:svip"), "svip")
    check("Чужой тариф не принимается", inline_keyboards.parse_tariff_key("bk:t:hacker") is None)
    check("Чужой префикс не принимается", inline_keyboards.parse_tariff_key("ad:ok:1") is None)
    check_equal("Разбор времени из callback", inline_keyboards.parse_time_key("bk:w:night"), "night")
    check_equal(
        "Разбор решения админа",
        inline_keyboards.parse_admin_decision("ad:no:7"),
        (BookingStatus.REJECTED, 7),
    )
    check("Решение с мусором вместо id отклоняется", inline_keyboards.parse_admin_decision("ad:ok:abc") is None)
    check_equal("Разбор страницы списка", inline_keyboards.parse_admin_list_page("ad:list:2"), 2)


# --------------------------------------------------------------------------- #
# 5. Тексты
# --------------------------------------------------------------------------- #
def test_texts() -> None:
    print("\n— Тексты —")
    price = texts.build_price_list()
    for title in ("Standart", "VIP", "S VIP", "Double"):
        check(f"Прайс содержит тариф {title}", title in price)
    check("Прайс содержит ночной тариф", "Ночь (22:00–08:00)" in price)
    check("Прайс содержит реквизиты", texts.PAYMENT_CARD in price)
    check("В прайсе не осталось плейсхолдеров", "{" not in price)

    rules = texts.build_rules_text()
    check("Правила собраны без плейсхолдеров", "{" not in rules and "Бронирование и оплата" in rules)
    contacts = texts.build_contacts_text()
    check("Контакты содержат телефон админа", texts.ADMIN_PHONE_PRETTY in contacts and "{" not in contacts)

    consent = texts.CONSENT_TEXT
    check("Согласие ссылается на 152-ФЗ", "152-ФЗ" in consent)
    check("Согласие перечисляет состав данных", "Telegram ID" in consent and "чека" in consent)
    check("Согласие описывает цель обработки", "Цель обработки" in consent)
    check("Согласие описывает отзыв", "отозв" in consent.lower())

    payment = texts.PAYMENT_DETAILS.format(
        tariff="VIP",
        time_slot="Сейчас",
        client_name="Иван",
        price_hint="180 ₽/час",
        payment_system=texts.PAYMENT_SYSTEM,
        payment_card=texts.PAYMENT_CARD,
        payment_holder=texts.PAYMENT_HOLDER,
    )
    check("Шаг оплаты содержит реквизиты Сбер + номер", "Сбер" in payment and texts.PAYMENT_CARD in payment)
    check("Шаг оплаты содержит имя получателя", texts.PAYMENT_HOLDER in payment)
    check("Шаг оплаты просит ФОТО чека", "ФОТО чека" in payment)

    samples = {
        texts.GREETING: {"user_name": "Иван"},
        texts.CHOOSE_TARIFF: {"tariffs_overview": texts.build_tariffs_overview()},
        texts.CHOOSE_TIME: {"tariff": "VIP", "now_time": "17:51", "now_date": "23 октября"},
        texts.ENTER_CUSTOM_TIME: {},
        texts.ENTER_NAME: {"tariff": "VIP", "time_slot": "Сейчас"},
        texts.RECEIPT_RECEIVED: {
            "booking_id": 1,
            "tariff": "VIP",
            "time_slot": "Сейчас",
            "client_name": "Иван",
        },
        texts.RECEIPT_NOT_PHOTO: {
            "payment_system": texts.PAYMENT_SYSTEM,
            "payment_card": texts.PAYMENT_CARD,
            "payment_holder": texts.PAYMENT_HOLDER,
        },
        texts.NEW_BOOKING_CAPTION: {
            "booking_id": 1,
            "tariff": "VIP",
            "time_slot": "Сейчас",
            "client_name": "Иван",
            "user_link": "@client",
            "created_at": "23.10.2026 17:51",
            "admins_count": 2,
        },
        texts.CLIENT_APPROVED: {
            "tariff": "VIP",
            "time_slot": "Сейчас",
            "booking_id": 1,
            "client_name": "Иван",
        },
        texts.CLIENT_REJECTED: {"booking_id": 1, "tariff": "VIP", "time_slot": "Сейчас"},
        texts.STATS_TEMPLATE: {
            "total": 10,
            "pending": 3,
            "approved": 5,
            "rejected": 2,
            "users": 8,
            "consented": 8,
            "admins": 2,
            "admins_active": 2,
        },
        texts.PENDING_ITEM: {
            "booking_id": 1,
            "tariff": "VIP",
            "time_slot": "Сейчас",
            "client_name": "Иван",
            "user_link": "@client",
            "created_at": "23.10.2026 17:51",
        },
        texts.ADMIN_ALREADY_PROCESSED: {"booking_id": 1, "status": "подтверждена"},
        texts.ADMIN_ALREADY_PROCESSED_NOTE: {"status": "подтверждена", "decider": "@admin"},
        texts.ADMIN_DECISION_APPROVED: {
            "decided_at": "23.10.2026 17:51",
            "decided_by": "@admin",
            "notify_status": "да",
        },
        texts.ADMIN_DECISION_REJECTED: {
            "decided_at": "23.10.2026 17:51",
            "decided_by": "@admin",
            "notify_status": "да",
        },
        texts.PEER_DECISION_NOTE: {
            "booking_id": 1,
            "tariff": "VIP",
            "time_slot": "Сейчас",
            "client_name": "Иван",
            "decision": BookingStatus.decision_title(BookingStatus.APPROVED),
            "decided_by": "@admin",
            "decided_at": "23.10.2026 17:51",
        },
        texts.ADMIN_PANEL_GREETING: {
            "club_name": texts.CLUB_NAME,
            "stats": texts.STATS_TEMPLATE.format(
                total=10, pending=3, approved=5, rejected=2, users=8, consented=8, admins=2,
                admins_active=2,
            ),
        },
        texts.ADMIN_START_INFO: {"role": "владелец"},
        texts.ADMINS_LIST_HEADER: {"total": 3, "active": 2},
        texts.ADMINS_ITEM: {
            "index": 1,
            "user_link": "@admin",
            "role": "владелец",
            "state": texts.ADMINS_STATE_ACTIVE,
        },
        texts.ADMINS_ITEM_ADDED: {
            "index": 2,
            "user_link": "@second",
            "role": "администратор",
            "state": texts.ADMINS_STATE_DISABLED,
            "added_by": "@admin",
        },
        texts.ADMIN_ADDED: {"user_link": "@new", "role": "администратор"},
        texts.ADMIN_ALREADY_EXISTS: {
            "user_link": "@new",
            "role": "администратор",
            "state": texts.ADMINS_STATE_ACTIVE,
        },
        texts.ADMIN_NOT_STARTED_WARNING: {"user_link": "@new"},
        texts.ADMIN_REMOVED: {"user_link": "@new"},
        texts.ADMIN_DISABLED: {"user_link": "@new"},
        texts.ADMIN_ENABLED: {"user_link": "@new"},
        texts.ADMIN_ROLE_PROMOTED: {"user_link": "@new"},
        texts.ADMIN_ROLE_DEMOTED: {"user_link": "@new"},
        texts.ADMIN_MANAGE_FORBIDDEN: {"role": "администратор"},
        texts.ADMIN_ADD_PROMPT: {},
        texts.ADMIN_ADD_INVALID_ID: {},
        texts.ADMIN_ADD_CANCELED: {},
        texts.ADMIN_OWNER_PROTECTED: {},
        texts.ADMIN_SELF_PROTECTED: {},
        texts.ADMIN_LAST_OWNER_PROTECTED: {},
        texts.ADMIN_NOT_ACTIVE: {},
        texts.ADMIN_NO_ACCESS: {},
        texts.ADMINS_EMPTY: {},
    }
    for template, payload in samples.items():
        rendered = template.format(**payload)
        check(f"Шаблон «{template.splitlines()[0][:38]}…» рендерится", "{" not in rendered)

    check("Подсказка цены для ночи точная", "850 ₽" in texts.tariff_price_hint("vip", "night"))
    check("Подсказка цены для «Сейчас» ссылается на прайс", "прайсу" in texts.tariff_price_hint("vip", "now"))
    check("Подпись кнопки тарифа содержит цену", "₽/час" in texts.tariff_button_label(texts.TARIFFS[0]))
    check(
        "Подпись к чеку укладывается в лимит Telegram (1024)",
        len(
            texts.NEW_BOOKING_CAPTION.format(
                booking_id=1,
                tariff="S VIP",
                time_slot="На ночь",
                client_name="И",
                user_link="@user",
                created_at="23.10.2026 17:51",
                admins_count=3,
            )
        )
        <= 1024,
    )


# --------------------------------------------------------------------------- #
# 6. Сквозной сценарий: клиент → админ
# --------------------------------------------------------------------------- #
async def test_full_scenario(db: Database, cfg: AppConfig, client_id: int) -> None:
    print("\n— Сквозной сценарий (клиент → админ) —")
    bot = BotSpy()
    state = make_fsm_context(client_id)

    # --- /start без согласия ------------------------------------------------ #
    mark = bot.mark()
    start_message = make_message(user_id=client_id, bot=bot, text="/start")
    await client_handlers.cmd_start(start_message, state, db, cfg)
    sent = bot.new_texts(mark)
    check("Без согласия /start показывает текст 152-ФЗ", any("152-ФЗ" in text for text in sent))
    consent_markup = markup_of(bot.new_messages(mark)[0])
    check(
        "Без согласия показывается только кнопка «✅ Я согласен»",
        getattr(consent_markup, "inline_keyboard", None) is not None
        and consent_markup.inline_keyboard[0][0].text == "✅ Я согласен",
    )
    check("Без согласия меню не выдаётся", "Прайс-лист" not in "".join(sent))

    # --- кнопка «Я согласен» ------------------------------------------------ #
    mark = bot.mark()
    consent_callback = make_callback(inline_keyboards.CB_CONSENT_GIVE, user_id=client_id, bot=bot)
    await client_handlers.cb_consent_give(consent_callback, state, db, bot, cfg)
    check("После кнопки согласие записано в базу", (await db.has_consent(client_id)) is True)
    edits = [p for n, p in bot.since(mark) if n == "EditMessageText"]
    check("Сообщение с согласием заменено подтверждением", edits and "Согласие" in text_of(edits[0]))
    greeting_payload = [p for n, p in bot.since(mark) if n == "SendMessage"][-1]
    check("После согласия показано приветствие", "Здравствуйте" in text_of(greeting_payload))
    check(
        "Вместе с приветствием выдано reply-меню из 4 кнопок",
        len([b for row in markup_of(greeting_payload).keyboard for b in row]) == 4,
    )
    check("Тост о фиксации согласия отправлен", any("Согласие зафиксировано" in text_of(p) for p in bot.answers()))

    # --- повторный /start уже с согласием ----------------------------------- #
    mark = bot.mark()
    await client_handlers.cmd_start(
        make_message(user_id=client_id, bot=bot, text="/start"), state, db, cfg
    )
    check("После согласия /start открывает меню", any("Меню" in t or "Здравствуйте" in t for t in bot.new_texts(mark)))

    # --- шаг 1: тариф ------------------------------------------------------- #
    mark = bot.mark()
    await client_handlers.msg_start_booking(
        make_message(user_id=client_id, bot=bot, text=reply_keyboards.MENU_BOOKING), state
    )
    check_equal("Шаг 1: состояние выбора тарифа", await state.get_state(), BookingFSM.choosing_tariff.state)
    tariff_message = bot.new_messages(mark)[0]
    check("Шаг 1 показывает тарифы", "Standart" in text_of(tariff_message) and "S VIP" in text_of(tariff_message))
    check(
        "Шаг 1 содержит inline-кнопки тарифов",
        len(markup_of(tariff_message).inline_keyboard) == 5,
    )

    mark = bot.mark()
    await client_handlers.cb_choose_tariff(
        make_callback(inline_keyboards.tariff_callback("svip"), user_id=client_id, bot=bot),
        state,
        bot,
        cfg,
    )
    check_equal("Шаг 2: состояние выбора времени", await state.get_state(), BookingFSM.choosing_time.state)
    check_equal("Тариф сохранён в FSM", (await state.get_data()).get(DATA_TARIFF_KEY), "svip")
    edited = [p for n, p in bot.since(mark) if n == "EditMessageText"]
    check("Сообщение шага 1 заменено шагом 2", edited and "шаг 2 из 4" in text_of(edited[0]))
    check("Шаг 2 показывает выбранное время клуба", "Сейчас в клубе" in text_of(edited[0]))

    # --- шаг 2: «Другое» → время текстом ------------------------------------ #
    await client_handlers.cb_choose_time(
        make_callback(inline_keyboards.time_callback("other"), user_id=client_id, bot=bot),
        state,
        bot,
        cfg,
    )
    check_equal(
        "Шаг 2б: состояние ввода своего времени",
        await state.get_state(),
        BookingFSM.entering_custom_time.state,
    )

    mark = bot.mark()
    await client_handlers.msg_enter_custom_time(
        make_message(user_id=client_id, bot=bot, text="когда-нибудь потом"), state
    )
    check("Время без цифр отклоняется подсказкой", bot.new_texts(mark) == [texts.INVALID_CUSTOM_TIME])
    check_equal(
        "После ошибки состояние не сбрасывается",
        await state.get_state(),
        BookingFSM.entering_custom_time.state,
    )

    await client_handlers.msg_enter_custom_time(
        make_message(user_id=client_id, bot=bot, text="сегодня в 18:30 на 3 часа"), state
    )
    check_equal("Шаг 3: состояние ввода имени", await state.get_state(), BookingFSM.entering_name.state)
    check_equal("Своё время сохранено", (await state.get_data()).get(DATA_TIME_SLOT), "сегодня в 18:30 на 3 часа")

    # --- кнопка меню во время сценария не ломает его ------------------------- #
    mark = bot.mark()
    await client_handlers.msg_enter_name(
        make_message(user_id=client_id, bot=bot, text=reply_keyboards.MENU_PRICE), state
    )
    check("Кнопка меню во время сценария даёт подсказку", any("оформляем бронь" in t for t in bot.new_texts(mark)))
    check_equal("Сценарий не прерывается", await state.get_state(), BookingFSM.entering_name.state)

    mark = bot.mark()
    await client_handlers.msg_enter_name(make_message(user_id=client_id, bot=bot, text="http://spam"), state)
    check("Некорректное имя отклоняется", bot.new_texts(mark) == [texts.INVALID_NAME])

    # --- шаг 3: имя → реквизиты --------------------------------------------- #
    mark = bot.mark()
    await client_handlers.msg_enter_name(make_message(user_id=client_id, bot=bot, text="тимур"), state)
    check_equal("Шаг 4: ожидание чека", await state.get_state(), BookingFSM.waiting_receipt.state)
    payment_text = bot.new_texts(mark)[0]
    check(
        "На шаге оплаты показаны реквизиты Сбер + номер",
        "Сбер" in payment_text and texts.PAYMENT_CARD in payment_text,
    )
    check("На шаге оплаты указано имя получателя", texts.PAYMENT_HOLDER in payment_text)
    check("На шаге оплаты просят ФОТО чека", "ФОТО чека" in payment_text)
    check("Имя нормализовано до «Тимур»", "Тимур" in payment_text)
    check("Тариф подставлен в шаг оплаты", "S VIP" in payment_text)

    # --- текст вместо фото --------------------------------------------------- #
    mark = bot.mark()
    await client_handlers.msg_receipt_expected(
        make_message(user_id=client_id, bot=bot, text="я оплатил")
    )
    check(
        "Текст вместо фото — просьба прислать именно фото",
        any("именно" in t and "ФОТО" in t for t in bot.new_texts(mark)),
    )

    # --- шаг 4: фото чека ---------------------------------------------------- #
    mark = bot.mark()
    await client_handlers.msg_receive_receipt(
        make_message(user_id=client_id, bot=bot, with_photo=True), state, db, bot, cfg
    )
    check("FSM очищен после создания заявки", (await state.get_state()) is None)
    client_reply = bot.new_texts(mark)[0]
    check("Клиенту: «Чек получен! Ожидайте подтверждения»", "Чек получен" in client_reply)
    check(
        "Клиенту вернули главное меню",
        "Прайс-лист" not in client_reply and markup_of(bot.new_messages(mark)[0]) is not None,
    )

    admin_photos = [p for n, p in bot.since(mark) if n == "SendPhoto"]
    check("Админу отправлено одно фото чека", len(admin_photos) == 1)
    admin_photo = admin_photos[0]
    check_equal("Уведомление ушло администратору из .env", admin_photo.chat_id, ADMIN_ID)
    check_equal("В базу сохранён file_id самого большого фото", admin_photo.photo, "AgAC_size_1280")
    caption = text_of(admin_photo)
    check("В подписи чека есть «НОВАЯ БРОНЬ»", "НОВАЯ БРОНЬ" in caption)
    check("В подписи есть тариф, время, имя", all(x in caption for x in ("S VIP", "18:30", "Тимур")))
    check("В подписи есть ссылка на клиента", "Юзер:" in caption)
    decision_markup = markup_of(admin_photo)
    check_equal(
        "Первая кнопка под чеком — «✅ Подтвердить»",
        decision_markup.inline_keyboard[0][0].text,
        "✅ Подтвердить",
    )
    check_equal("Вторая кнопка под чеком — «❌ Отклонить»", decision_markup.inline_keyboard[0][1].text, "❌ Отклонить")
    check(
        "В кнопки зашит id брони",
        inline_keyboards.parse_admin_decision(decision_markup.inline_keyboard[0][0].callback_data)[1] > 0,
    )

    created = await db.list_bookings(BookingStatus.PENDING, limit=200, oldest_first=True)
    target = next(b for b in created if b.client_name == "Тимур")
    check_equal("Заявка сохранена со статусом pending", target.status, BookingStatus.PENDING)
    check_equal("В заявке сохранён тариф", target.tariff, "S VIP")

    # --- админ: список активных броней --------------------------------------- #
    mark = bot.mark()
    await admin_handlers.cb_pending_list(
        make_callback(inline_keyboards.admin_list_callback(0), user_id=ADMIN_ID, bot=bot), db, bot, cfg
    )
    edits = [p for n, p in bot.since(mark) if n in ("EditMessageText", "SendMessage")]
    list_text = text_of(edits[-1])
    pending_total = await db.count_bookings(BookingStatus.PENDING)
    check("Список активных броней отрисован", "Активные брони" in list_text)
    check("В списке видны номера заявок", "№" in list_text)
    check("Счётчик в заголовке совпадает с базой", f"Заявок: <b>{pending_total}</b>" in list_text)
    check("На странице не больше 5 заявок", list_text.count("👤") <= 5)
    check(
        "У каждой заявки в списке есть кнопки решения",
        len(markup_of(edits[-1]).inline_keyboard) >= 2,
    )

    # --- админ: подтверждение брони ------------------------------------------ #
    mark = bot.mark()
    await admin_handlers.cb_decision(
        make_callback(
            inline_keyboards.admin_approve_callback(target.id),
            user_id=ADMIN_ID,
            bot=bot,
            with_photo=True,
            caption=f"🔔 <b>НОВАЯ БРОНЬ №{target.id}</b>",
        ),
        db,
        bot,
        cfg,
    )
    check_equal("Статус заявки — approved", (await db.get_booking(target.id)).status, BookingStatus.APPROVED)

    client_notifications = [p for n, p in bot.since(mark) if n == "SendMessage"]
    check("Клиенту отправлено одно уведомление", len(client_notifications) == 1)
    client_text = text_of(client_notifications[0])
    check_equal("Уведомление ушло клиенту", client_notifications[0].chat_id, client_id)
    check("Клиенту: «Ваша бронь подтверждена!»", "бронь подтверждена" in client_text)
    check("Клиенту показаны место и время", "S VIP" in client_text and "18:30" in client_text)

    caption_edits = [p for n, p in bot.since(mark) if n == "EditMessageCaption"]
    check("Подпись чека у админа обновлена", bool(caption_edits))
    check("В подписи появилась отметка «ПОДТВЕРЖДЕНО»", "ПОДТВЕРЖДЕНО" in text_of(caption_edits[0]))
    check("Кнопки решения убраны после подтверждения", markup_of(caption_edits[0]) is None)
    check(
        "Админ получил тост о подтверждении",
        any("подтверждена" in text_of(p) for p in [q for n, q in bot.since(mark) if n == "AnswerCallbackQuery"]),
    )

    # --- повторное нажатие той же кнопки ------------------------------------- #
    mark = bot.mark()
    await admin_handlers.cb_decision(
        make_callback(
            inline_keyboards.admin_approve_callback(target.id),
            user_id=ADMIN_ID,
            bot=bot,
            with_photo=True,
            caption=f"🔔 <b>НОВАЯ БРОНЬ №{target.id}</b>\n\n✅ <b>ПОДТВЕРЖДЕНО</b>",
        ),
        db,
        bot,
        cfg,
    )
    check(
        "Повторное подтверждение не дублирует уведомление клиенту",
        not [p for n, p in bot.since(mark) if n == "SendMessage"],
    )
    alerts = [p for n, p in bot.since(mark) if n == "AnswerCallbackQuery"]
    check("Админ получил alert о повторном решении", alerts and alerts[0].show_alert is True)
    check("В alert указан текущий статус", alerts and "подтверждена" in text_of(alerts[0]))

    # --- админ: отклонение другой заявки (из списка, без фото) --------------- #
    other_booking = await db.add_booking(
        client_id, "Double", "Сейчас (с 22:00, 23.10.2026)", "Олег", "AgAC_second"
    )
    mark = bot.mark()
    await admin_handlers.cb_decision(
        make_callback(inline_keyboards.admin_reject_callback(other_booking.id), user_id=ADMIN_ID, bot=bot),
        db,
        bot,
        cfg,
    )
    check_equal(
        "Статус отклонённой заявки — rejected",
        (await db.get_booking(other_booking.id)).status,
        BookingStatus.REJECTED,
    )
    rejected_text = text_of([p for n, p in bot.since(mark) if n == "SendMessage"][0])
    check("При отклонении клиенту дан телефон админа", texts.ADMIN_PHONE_PRETTY in rejected_text)
    check("При отклонении сказано, что бронь не подтверждена", "не смогли подтвердить" in rejected_text)
    check(
        "Сообщение админа со списком перерисовано",
        any(n == "EditMessageText" for n, _ in bot.since(mark)),
    )

    # --- админ: /admin со статистикой ---------------------------------------- #
    mark = bot.mark()
    await admin_handlers.cmd_admin(
        make_message(user_id=ADMIN_ID, bot=bot, text="/admin", username="admin", first_name="Админ"),
        db,
    )
    stats_payload = bot.new_messages(mark)[0]
    stats_text = text_of(stats_payload)
    check("Статистика содержит «Всего заявок»", "Всего заявок" in stats_text)
    check("Статистика содержит «Ожидают»", "Ожидают" in stats_text)
    check("Статистика содержит «Подтверждено»", "Подтверждено" in stats_text)
    check("Статистика содержит «Отклонено»", "Отклонено" in stats_text)
    check("Статистика показывает число пользователей", "Пользователей в базе" in stats_text)
    check(
        "Панель содержит кнопку «📋 Активные брони»",
        any(b.text == "📋 Активные брони" for row in markup_of(stats_payload).inline_keyboard for b in row),
    )

    mark = bot.mark()
    await admin_handlers.cb_refresh_stats(
        make_callback(inline_keyboards.CB_ADMIN_STATS, user_id=ADMIN_ID, bot=bot), db, bot
    )
    check("Кнопка обновления статистики работает", any(n == "EditMessageText" for n, _ in bot.since(mark)))

    await state.storage.close()


# --------------------------------------------------------------------------- #
# 7. Отмена и протухшие кнопки
# --------------------------------------------------------------------------- #
async def test_cancel_and_outdated(db: Database, cfg: AppConfig) -> None:
    print("\n— Отмена и устаревшие кнопки —")
    bot = BotSpy()
    state = make_fsm_context(CANCEL_CLIENT_ID)
    await state.set_state(BookingFSM.entering_name)

    mark = bot.mark()
    await client_handlers.cmd_cancel(
        make_message(user_id=CANCEL_CLIENT_ID, bot=bot, text="/cancel"), state
    )
    check("После /cancel состояние сброшено", (await state.get_state()) is None)
    check("Клиенту сообщено об отмене", bot.new_texts(mark) == [texts.BOOKING_CANCELED])
    check(
        "После отмены вернули reply-меню",
        markup_of(bot.new_messages(mark)[0]).keyboard[0][0].text == reply_keyboards.MENU_PRICE,
    )

    mark = bot.mark()
    await client_handlers.cmd_cancel(
        make_message(user_id=CANCEL_CLIENT_ID, bot=bot, text="/cancel"), state
    )
    check(
        "/cancel без активного бронирования отвечает корректно",
        bot.new_texts(mark) == [texts.BOOKING_NOT_STARTED],
    )

    mark = bot.mark()
    await client_handlers.cb_cancel_booking(
        make_callback(inline_keyboards.CB_BOOKING_CANCEL, user_id=CANCEL_CLIENT_ID, bot=bot), state, bot
    )
    check("Кнопка «Отменить» удаляет сообщение с кнопками", any(n == "DeleteMessage" for n, _ in bot.since(mark)))
    check("После кнопки отмены состояние сброшено", (await state.get_state()) is None)

    await state.set_state(BookingFSM.choosing_tariff)
    mark = bot.mark()
    outdated = make_callback(inline_keyboards.tariff_callback("vip"), user_id=CANCEL_CLIENT_ID, bot=bot)
    await client_handlers.cb_booking_outdated(outdated)
    alerts = [p for n, p in bot.since(mark) if n == "AnswerCallbackQuery"]
    check("Протухшая кнопка бронирования даёт alert", alerts and alerts[0].show_alert is True)
    await state.clear()

    mark = bot.mark()
    await client_handlers.msg_photo_without_booking(
        make_message(user_id=CANCEL_CLIENT_ID, bot=bot, with_photo=True), state
    )
    check("Фото вне бронирования объясняет, что делать", bot.new_texts(mark) == [texts.PHOTO_WITHOUT_BOOKING])

    # Фото, присланное посреди сценария, не должно обрывать бронирование
    await state.set_state(BookingFSM.entering_name)
    mark = bot.mark()
    await client_handlers.msg_photo_without_booking(
        make_message(user_id=CANCEL_CLIENT_ID, bot=bot, with_photo=True), state
    )
    check("Фото на шаге ввода имени даёт подсказку про имя", "указать имя" in bot.new_texts(mark)[0])
    check_equal("Сценарий при этом продолжается", await state.get_state(), BookingFSM.entering_name.state)
    await state.clear()

    mark = bot.mark()
    await client_handlers.msg_unknown(make_message(user_id=CANCEL_CLIENT_ID, bot=bot, text="что-то странное"))
    check("Непонятное сообщение получает подсказку про меню", bot.new_texts(mark) == [texts.UNKNOWN_MESSAGE])

    mark = bot.mark()
    await client_handlers.msg_price_list(
        make_message(user_id=CANCEL_CLIENT_ID, bot=bot, text=reply_keyboards.MENU_PRICE)
    )
    price_text = bot.new_texts(mark)[0]
    check("Прайс-лист отправляется клиенту", "ПРАЙС-ЛИСТ" in price_text and "Standart" in price_text)

    mark = bot.mark()
    await client_handlers.msg_rules(make_message(user_id=CANCEL_CLIENT_ID, bot=bot, text=reply_keyboards.MENU_RULES))
    check("Правила отправляются клиенту", "ПРАВИЛА КЛУБА" in bot.new_texts(mark)[0])

    mark = bot.mark()
    await client_handlers.msg_contacts(
        make_message(user_id=CANCEL_CLIENT_ID, bot=bot, text=reply_keyboards.MENU_CONTACTS)
    )
    contacts_calls = [p for n, p in bot.since(mark) if n in ("SendMessage", "SendContact")]
    check("Контакты: текст + карточка контакта", len(contacts_calls) == 2)
    check("В контактах телефон администратора", texts.ADMIN_PHONE_PRETTY in text_of(contacts_calls[0]))
    check("Карточка контакта содержит номер", getattr(contacts_calls[1], "phone_number", "") == texts.ADMIN_PHONE_RAW)

    mark = bot.mark()
    await client_handlers.cmd_privacy(make_message(user_id=CANCEL_CLIENT_ID, bot=bot, text="/privacy"))
    check("Команда /privacy показывает политику ПДн", "152-ФЗ" in bot.new_texts(mark)[0])

    mark = bot.mark()
    await client_handlers.cmd_help(make_message(user_id=CANCEL_CLIENT_ID, bot=bot, text="/help"))
    check("Команда /help показывает справку", "Как пользоваться ботом" in bot.new_texts(mark)[0])

    await state.storage.close()



# --------------------------------------------------------------------------- #
# 8. Деградация: Telegram не дал отредактировать сообщение
# --------------------------------------------------------------------------- #
class NoEditBot(BotSpy):
    """«Бот», у которого правка сообщений всегда заканчивается ошибкой Telegram."""

    async def edit_message_text(self, **kwargs: Any) -> None:
        raise TelegramBadRequest(
            EditMessageText(chat_id=1, message_id=1, text="x"),
            "Bad Request: message can't be edited",
        )


async def test_edit_fallback(db: Database, cfg: AppConfig) -> None:
    print("\n— Отказоустойчивость правки сообщений —")
    bot = NoEditBot()
    state = make_fsm_context(CANCEL_CLIENT_ID + 1)

    await client_handlers.msg_start_booking(
        make_message(user_id=CANCEL_CLIENT_ID + 1, bot=bot, text=reply_keyboards.MENU_BOOKING), state
    )
    mark = bot.mark()
    await client_handlers.cb_choose_tariff(
        make_callback(inline_keyboards.tariff_callback("vip"), user_id=CANCEL_CLIENT_ID + 1, bot=bot),
        state,
        bot,
        cfg,
    )
    check(
        "Если правка не удалась, шаг 2 отправляется новым сообщением",
        any("шаг 2 из 4" in text for text in bot.new_texts(mark)),
    )
    check_equal("Сценарий при этом не сбивается", await state.get_state(), BookingFSM.choosing_time.state)

    mark = bot.mark()
    await admin_handlers.cb_pending_list(
        make_callback(inline_keyboards.admin_list_callback(0), user_id=ADMIN_ID, bot=bot), db, bot, cfg
    )
    check(
        "Список заявок админа тоже уходит новым сообщением при отказе правки",
        any("Активные брони" in text or "Активных броней нет" in text for text in bot.new_texts(mark)),
    )
    await state.clear()
    await state.storage.close()


# --------------------------------------------------------------------------- #
# 9. Роутеры, фильтры, middleware
# --------------------------------------------------------------------------- #
def build_dispatcher(
    db: Database, cfg: AppConfig, admins: AdminRegistry | None = None
) -> Dispatcher:
    """Собрать диспетчер ровно так, как это делает :mod:`main`."""
    dispatcher = Dispatcher(storage=MemoryStorage(), db=db, cfg=cfg, admins=admins)
    dispatcher.include_router(admin_handlers.router)   # админ-панель — первой
    dispatcher.include_router(client_handlers.router)  # клиент — вторым
    return dispatcher


def root_filters(observer: Any) -> list[Any]:
    """Роутовые фильтры наблюдателя aiogram (внутреннее поле — только для проверки)."""
    handler = getattr(observer, "_handler", None)
    return [getattr(item, "callback", item) for item in (getattr(handler, "filters", None) or [])]


async def test_wiring(db: Database, cfg: AppConfig, dispatcher: Dispatcher) -> None:
    print("\n— Сборка роутеров и фильтров —")
    check("Админ-роутер подключён первым (приоритет панели)", dispatcher.sub_routers[0].name == "admin")
    check("Клиентский роутер подключён вторым", dispatcher.sub_routers[1].name == "client")

    client_message_handlers = len(client_handlers.router.message.handlers)
    client_callback_handlers = len(client_handlers.router.callback_query.handlers)
    check("Клиентские хендлеры сообщений зарегистрированы", client_message_handlers >= 12, str(client_message_handlers))
    check("Клиентские хендлеры кнопок зарегистрированы", client_callback_handlers >= 4, str(client_callback_handlers))
    check("Админские хендлеры сообщений зарегистрированы", len(admin_handlers.router.message.handlers) >= 1)
    check("Админские хендлеры кнопок зарегистрированы", len(admin_handlers.router.callback_query.handlers) >= 3)

    check(
        "На клиентском роутере стоит middleware согласия (152-ФЗ)",
        any(isinstance(m, ConsentMiddleware) for m in client_handlers.router.message.outer_middleware),
    )
    check(
        "Middleware согласия стоит и на кнопках",
        any(isinstance(m, ConsentMiddleware) for m in client_handlers.router.callback_query.outer_middleware),
    )
    check(
        "Админ-роутер защищён фильтром IsAdmin",
        any(isinstance(f, IsAdmin) for f in root_filters(admin_handlers.router.message))
        and any(isinstance(f, IsAdmin) for f in root_filters(admin_handlers.router.callback_query)),
    )
    check(
        "Роутеры работают только в личных чатах",
        any(isinstance(f, IsPrivateChat) for f in root_filters(client_handlers.router.message)),
    )

    update_types = set(dispatcher.resolve_used_update_types())
    check(
        "Бот подписан только на нужные типы апдейтов",
        update_types == {"message", "callback_query"},
        str(update_types),
    )

    # --- фильтры в деле ------------------------------------------------------ #
    is_admin = IsAdmin()
    admin_sender = SimpleNamespace(from_user=SimpleNamespace(id=ADMIN_ID))
    client_sender = SimpleNamespace(from_user=SimpleNamespace(id=DB_CLIENT_ID))
    check("IsAdmin пропускает администратора", (await is_admin(admin_sender, cfg=cfg)) is True)
    check("IsAdmin не пропускает клиента", (await is_admin(client_sender, cfg=cfg)) is False)
    check("IsAdmin без конфигурации запрещает доступ", (await is_admin(admin_sender, cfg=None)) is False)

    is_private = IsPrivateChat()
    private_chat = SimpleNamespace(type="private")
    group_chat = SimpleNamespace(type="group")
    check("IsPrivateChat пропускает личный чат", (await is_private(object(), event_chat=private_chat)) is True)
    check("IsPrivateChat отсекает группу", (await is_private(object(), event_chat=group_chat)) is False)
    check("IsPrivateChat без чата отсекает событие", (await is_private(object(), event_chat=None)) is False)

    # --- middleware согласия: блокирует меню и пропускает /start ------------- #
    gate = ConsentMiddleware()
    newbie_id = 424_242_424
    calls: list[str] = []

    async def handler(event: Any, data: dict[str, Any]) -> str:
        calls.append("handler")
        return "handled"

    context: dict[str, Any] = {
        "db": db,
        "event_from_user": TelegramUser(id=newbie_id, is_bot=False, first_name="Новичок"),
        "event_chat": Chat(id=newbie_id, type="private"),
    }
    bot = BotSpy()

    menu_event = make_message(user_id=newbie_id, bot=bot, text=reply_keyboards.MENU_PRICE).as_(bot)
    result = await gate(handler, menu_event, dict(context))
    check("Без согласия меню не работает (хендлер не вызван)", result is None and not calls)
    check("Без согласия показывается напоминание про 152-ФЗ", any("152-ФЗ" in t for t in bot.texts()))

    mark = bot.mark()
    start_event = make_message(user_id=newbie_id, bot=bot, text="/start").as_(bot)
    await gate(handler, start_event, dict(context))
    check("Команда /start проходит через middleware согласия", calls == ["handler"])
    check("При пропуске /start middleware ничего не отправляет", bot.new_texts(mark) == [])

    await db.upsert_user(newbie_id, "newbie", "Новичок")
    await db.set_consent(newbie_id, True)
    mark = bot.mark()
    menu_after_consent = make_message(user_id=newbie_id, bot=bot, text=reply_keyboards.MENU_PRICE)
    await gate(handler, menu_after_consent, dict(context))
    check("После согласия middleware пропускает события", len(calls) == 2)
    check("После согласия middleware не шлёт напоминаний", bot.new_texts(mark) == [])

    # Кнопки до согласия
    blocked_callback = make_callback(inline_keyboards.tariff_callback("vip"), user_id=newbie_id + 1, bot=bot)
    blocked_result = await gate(
        handler,
        blocked_callback,
        {**context, "event_from_user": TelegramUser(id=newbie_id + 1, is_bot=False, first_name="Ещё один")},
    )
    check("Кнопки без согласия блокируются", blocked_result is None)
    check(
        "Блокировка кнопки сопровождается alert'ом",
        any(getattr(p, "show_alert", False) for p in bot.answers()),
    )

    consent_callback = make_callback(inline_keyboards.CB_CONSENT_GIVE, user_id=newbie_id + 1, bot=bot)
    before = len(calls)
    await gate(
        handler,
        consent_callback,
        {**context, "event_from_user": TelegramUser(id=newbie_id + 1, is_bot=False, first_name="Ещё один")},
    )
    check("Кнопка «Я согласен» проходит через middleware", len(calls) == before + 1)

    # --- Bot с HTML-разметкой (без сетевых вызовов) -------------------------- #
    real_bot = Bot(token=TEST_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    check("Bot создаётся с parse_mode=HTML", real_bot.default.parse_mode == ParseMode.HTML)
    await real_bot.session.close()



# --------------------------------------------------------------------------- #
# 10. Маршрутизация настоящих апдейтов через Dispatcher
# --------------------------------------------------------------------------- #
def make_update(counter: int, event: Any) -> Any:
    """Собрать настоящий :class:`aiogram.types.Update` вокруг события."""
    from aiogram.types import Update

    if isinstance(event, CallbackQuery):
        return Update(update_id=counter, callback_query=event)
    return Update(update_id=counter, message=event)


async def test_dispatcher_routing(db: Database, cfg: AppConfig, dispatcher: Dispatcher) -> None:
    """Прогнать апдейты через собранный диспетчер — как в работающем боте.

    Проверяется вся обвязка: порядок роутеров, фильтр IsAdmin, фильтр приватного
    чата, middleware согласия, Command/StateFilter/F.data и общий FSM-контекст
    между апдейтами одного пользователя.
    """
    print("\n— Маршрутизация реальных апдейтов (Dispatcher.feed_update) —")
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot = BotSpy()
    user_id = 313_313_313
    counter = 0

    async def feed(event: Any) -> tuple[Any, list[str]]:
        """Отправить апдейт боту и вернуть (результат, новые тексты)."""
        nonlocal counter
        counter += 1
        mark = bot.mark()
        result = await dispatcher.feed_update(bot, make_update(counter, event))
        return result, bot.step_texts(mark)

    # 1. /start от нового пользователя → согласие 152-ФЗ
    _, sent = await feed(make_message(user_id=user_id, bot=bot, text="/start"))
    check("feed_update: /start показывает согласие 152-ФЗ", any("152-ФЗ" in t for t in sent))
    check("feed_update: до согласия меню не выдаётся", not any("Прайс-лист" in t for t in sent))

    # 2. Кнопка меню без согласия → блокировка middleware
    _, sent = await feed(make_message(user_id=user_id, bot=bot, text=reply_keyboards.MENU_PRICE))
    check("feed_update: без согласия меню заблокировано", any("152-ФЗ" in t for t in sent))
    check("feed_update: прайс без согласия не показан", not any("ПРАЙС-ЛИСТ" in t for t in sent))

    # 3. Согласие через callback
    await feed(make_callback(inline_keyboards.CB_CONSENT_GIVE, user_id=user_id, bot=bot))
    check("feed_update: после кнопки согласие в базе", (await db.has_consent(user_id)) is True)

    # 4. Меню работает
    _, sent = await feed(make_message(user_id=user_id, bot=bot, text=reply_keyboards.MENU_PRICE))
    check("feed_update: с согласием прайс отправляется", any("ПРАЙС-ЛИСТ" in t for t in sent))

    # 5. Полный сценарий бронирования через апдейты (FSM общий между ними)
    _, sent = await feed(make_message(user_id=user_id, bot=bot, text=reply_keyboards.MENU_BOOKING))
    check("feed_update: старт бронирования показывает тарифы", any("Standart" in t for t in sent))

    _, sent = await feed(make_callback(inline_keyboards.tariff_callback("double"), user_id=user_id, bot=bot))
    check("feed_update: шаг 2 — выбор времени", any("шаг 2 из 4" in t for t in sent))

    _, sent = await feed(make_callback(inline_keyboards.time_callback("night"), user_id=user_id, bot=bot))
    check("feed_update: шаг 3 — ввод имени", any("шаг 3 из 4" in t for t in sent))
    check("feed_update: ночной тариф подставлен в шаг", any("22:00–08:00" in t for t in sent))

    _, sent = await feed(make_message(user_id=user_id, bot=bot, text="алексей"))
    check("feed_update: шаг 4 — реквизиты и запрос чека", any("ФОТО чека" in t for t in sent))
    check("feed_update: имя нормализовано", any("Алексей" in t for t in sent))

    _, sent = await feed(make_message(user_id=user_id, bot=bot, text="а где реквизиты?"))
    check("feed_update: текст вместо чека — просьба прислать фото", any("именно" in t for t in sent))

    _, sent = await feed(make_message(user_id=user_id, bot=bot, with_photo=True))
    check("feed_update: чек принят", any("Чек получен" in t for t in sent))
    admin_photos = [p for n, p in bot.calls if n == "SendPhoto" and getattr(p, "chat_id", None) == ADMIN_ID]
    check("feed_update: чек ушёл администратору", bool(admin_photos))

    pending = await db.list_bookings(BookingStatus.PENDING, limit=50, oldest_first=True)
    live = next(b for b in pending if b.client_name == "Алексей")
    check_equal("feed_update: заявка в базе со статусом pending", live.status, BookingStatus.PENDING)

    # 6. Админ подтверждает бронь через апдейт
    mark = bot.mark()
    await feed(
        make_callback(
            inline_keyboards.admin_approve_callback(live.id),
            user_id=ADMIN_ID,
            bot=bot,
            with_photo=True,
            caption=f"🔔 <b>НОВАЯ БРОНЬ №{live.id}</b>",
        )
    )
    check_equal("feed_update: статус стал approved", (await db.get_booking(live.id)).status, BookingStatus.APPROVED)
    client_notify = [
        p for n, p in bot.since(mark) if n == "SendMessage" and getattr(p, "chat_id", None) == user_id
    ]
    check("feed_update: клиенту ушло подтверждение", any("бронь подтверждена" in text_of(p) for p in client_notify))

    # 7. /admin: доступ только у администратора
    _, sent = await feed(make_message(user_id=user_id, bot=bot, text="/admin"))
    check("feed_update: /admin от клиента не показывает статистику", not any("Всего заявок" in t for t in sent))
    check(
        "feed_update: /admin от клиента объясняет отсутствие прав",
        any("доступна администраторам" in t for t in sent),
    )
    _, sent = await feed(make_message(user_id=ADMIN_ID, bot=bot, text="/admin", username="admin", first_name="Админ"))
    check("feed_update: /admin администратору показывает статистику", any("Всего заявок" in t for t in sent))

    # 7b. Администратор, добавленный через панель, получает доступ без перезапуска
    registry: AdminRegistry = dispatcher.workflow_data["admins"]
    await db.add_admin(ADMIN_ID_2, role=AdminRole.OPERATOR, added_by=ADMIN_ID)
    registry.remember(ADMIN_ID_2)

    _, sent = await feed(make_message(user_id=user_id, bot=bot, text="/admins"))
    check("feed_update: /admins от клиента не показывает список", not any("Администраторы клуба" in t for t in sent))

    mark = bot.mark()
    _, sent = await feed(
        make_message(user_id=ADMIN_ID_2, bot=bot, text="/admins", username="second_admin", first_name="Второй")
    )
    check(
        "feed_update: добавленный админ видит список администраторов",
        any("Администраторы клуба" in t for t in sent),
    )
    check(
        "feed_update: оператору объяснено, что управление — у владельца",
        any("только владелец" in t for t in sent),
    )
    operator_payload = bot.new_messages(mark)[0]
    check(
        "feed_update: у оператора нет кнопок управления администраторами",
        not any(
            b.callback_data and b.callback_data.startswith("ad:admin:")
            for row in markup_of(operator_payload).inline_keyboard
            for b in row
        ),
    )

    mark = bot.mark()
    await feed(make_message(user_id=ADMIN_ID, bot=bot, text="/admins", username="admin", first_name="Админ"))
    owner_payload = bot.new_messages(mark)[0]
    check(
        "feed_update: у владельца есть кнопка «➕ Добавить»",
        any(b.text == "➕ Добавить" for row in markup_of(owner_payload).inline_keyboard for b in row),
    )

    # Добавление администратора через апдейты: кнопка → ID → запись в базу
    mark = bot.mark()
    await feed(make_callback(inline_keyboards.CB_ADMIN_ADD, user_id=ADMIN_ID, bot=bot))
    check(
        "feed_update: владелец получил запрос ID нового администратора",
        any("числовой Telegram ID" in t for t in bot.step_texts(mark)),
    )
    _, sent = await feed(
        make_message(user_id=ADMIN_ID, bot=bot, text=str(ADMIN_ID_3), username="admin", first_name="Админ")
    )
    check("feed_update: новый администратор добавлен", await db.get_admin(ADMIN_ID_3) is not None)
    check("feed_update: после добавления показан обновлённый список", any("Администраторы клуба" in t for t in sent))

    _, sent = await feed(
        make_message(user_id=ADMIN_ID_3, bot=bot, text="/admin", username="new_admin", first_name="Новый")
    )
    check(
        "feed_update: добавленный через апдейты админ видит статистику",
        any("Всего заявок" in t for t in sent),
    )

    # Убираем за собой: дальше эти администраторы проверяются отдельно
    await db.remove_admin(ADMIN_ID_3)
    await db.remove_admin(ADMIN_ID_2)
    registry.forget(ADMIN_ID_3)
    registry.forget(ADMIN_ID_2)

    # 8. Группа: бот молчит (фильтр приватного чата)
    group_message = Message(
        message_id=9,
        date=datetime.now(TEST_TZ),
        chat=Chat(id=-100_111, type="group", title="Клуб"),
        from_user=TelegramUser(id=user_id, is_bot=False, first_name="Клиент"),
        text="/start",
    )
    counter += 1
    group_result = await dispatcher.feed_update(bot, make_update(counter, group_message))
    check("feed_update: в группе бот не отвечает", group_result is UNHANDLED)


# --------------------------------------------------------------------------- #
# 11. Несколько администраторов: база, реестр, панель управления
# --------------------------------------------------------------------------- #
def _event(user_id: int) -> Any:
    """Минимальное «событие» для проверки фильтра доступа."""
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id))


async def test_multi_admin(db: Database) -> None:
    print("\n— Несколько администраторов —")
    cfg = make_cfg(db.path, admin_ids=(ADMIN_ID, ADMIN_ID_2))
    admins = AdminRegistry(db, cfg)
    synced = await admins.startup()

    # --- синхронизация .env с базой ----------------------------------------- #
    check_equal("ADMIN_IDS из .env синхронизированы с базой", synced, (ADMIN_ID, ADMIN_ID_2))
    owner = await db.get_admin(ADMIN_ID)
    second = await db.get_admin(ADMIN_ID_2)
    check("Первый ID из .env стал владельцем", owner is not None and owner.is_owner)
    check(
        "Второй ID из .env стал администратором",
        second is not None and second.role == AdminRole.OPERATOR,
    )
    check("Оба администратора активны", owner.is_active and second.is_active)
    check_equal("Роль владельца называется по-русски", owner.role_title, "владелец")
    check_equal("Повторный startup идемпотентен", await admins.startup(), (ADMIN_ID, ADMIN_ID_2))
    check_equal("В базе два активных администратора", await db.count_admins(), (2, 2))
    check_equal(
        "Список активных администраторов из базы",
        set(await db.active_admin_ids()),
        {ADMIN_ID, ADMIN_ID_2},
    )

    # --- доступ ------------------------------------------------------------- #
    is_admin = IsAdmin()
    check(
        "Владелец проходит фильтр доступа",
        await is_admin(_event(ADMIN_ID), admins=admins, cfg=cfg, db=db),
    )
    check(
        "Второй администратор проходит фильтр доступа",
        await is_admin(_event(ADMIN_ID_2), admins=admins, cfg=cfg, db=db),
    )
    check(
        "Клиент не проходит фильтр доступа",
        not await is_admin(_event(DB_CLIENT_ID), admins=admins, cfg=cfg, db=db),
    )
    check("Фильтр работает и без реестра (по базе)", await is_admin(_event(ADMIN_ID_2), cfg=cfg, db=db))
    check("Реестр знает владельца", await admins.is_owner(ADMIN_ID))
    check("Обычный администратор — не владелец", not await admins.is_owner(ADMIN_ID_2))

    await db.set_admin_active(ADMIN_ID_2, False)
    admins.forget(ADMIN_ID_2)
    check(
        "Отключённый администратор теряет доступ",
        not await is_admin(_event(ADMIN_ID_2), admins=admins, cfg=cfg, db=db),
    )
    check("Отключённого нет в списке активных", ADMIN_ID_2 not in await db.active_admin_ids())
    await db.set_admin_active(ADMIN_ID_2, True)
    admins.remember(ADMIN_ID_2)
    check(
        "Включённый администратор снова проходит фильтр",
        await is_admin(_event(ADMIN_ID_2), admins=admins, cfg=cfg, db=db),
    )

    # --- рассылка заявки всем администраторам -------------------------------- #
    bot = BotSpy()
    bot.profiles[ADMIN_ID_2] = SimpleNamespace(
        id=ADMIN_ID_2, username="second_admin", full_name="Второй"
    )
    await db.upsert_user(MULTI_CLIENT_ID, "multi_client", "Мультиклиент")
    await db.set_consent(MULTI_CLIENT_ID, granted=True)
    booking = await db.add_booking(
        MULTI_CLIENT_ID, "VIP", "Сейчас (с 20:00, 23.10.2026)", "Мультиклиент", "AgAC_multi"
    )
    client_user = await db.get_user(MULTI_CLIENT_ID)

    check_equal(
        "Получатели уведомлений берутся из базы",
        set(await admin_recipients(db, cfg)),
        {ADMIN_ID, ADMIN_ID_2},
    )
    report = await deliver_new_booking(bot, cfg, db, booking, client_user)
    check("Отчёт рассылки успешный", isinstance(report, DeliveryReport) and report.ok)
    check_equal(
        "Заявка доставлена обоим администраторам",
        set(report.delivered),
        {ADMIN_ID, ADMIN_ID_2},
    )
    check_equal("Ошибок доставки нет", report.failed, ())
    photos = bot.select("SendPhoto")
    check_equal("Каждому админу ушла своя копия чека", len(photos), 2)
    check_equal("Первая копия — владельцу", photos[0].chat_id, ADMIN_ID)
    check_equal("Вторая копия — второму администратору", photos[1].chat_id, ADMIN_ID_2)
    check(
        "В подписи видно число уведомлённых администраторов",
        "Уведомлено администраторов: <b>2</b>" in text_of(photos[0]),
    )
    check("В подписи есть «НОВАЯ БРОНЬ»", "НОВАЯ БРОНЬ" in text_of(photos[0]))
    check(
        "У каждой копии свои кнопки решения",
        all(markup_of(photo) is not None for photo in photos),
    )
    for admin_id, message_id in report.notifications:
        await db.add_notification(booking.id, admin_id, message_id)
    check_equal("Копии уведомлений сохранены в базе", len(await db.list_notifications(booking.id)), 2)

    # --- решение принимает второй администратор ------------------------------ #
    peer_message_id = dict(report.notifications)[ADMIN_ID]
    mark = bot.mark()
    await admin_handlers.cb_decision(
        make_callback(
            inline_keyboards.admin_approve_callback(booking.id),
            user_id=ADMIN_ID_2,
            bot=bot,
            with_photo=True,
            caption=f"🔔 <b>НОВАЯ БРОНЬ №{booking.id}</b>",
        ),
        db,
        bot,
        cfg,
    )
    decided = await db.get_booking(booking.id)
    check_equal("Статус после решения второго админа", decided.status, BookingStatus.APPROVED)
    check_equal("В базе записано, кто подтвердил", decided.decided_by, ADMIN_ID_2)
    check("В базе записано, когда подтвердили", bool(decided.decided_at))

    client_notes = [
        payload for payload in bot.new_messages(mark) if getattr(payload, "chat_id", None) == MULTI_CLIENT_ID
    ]
    check(
        "Клиенту ушло уведомление о решении",
        bool(client_notes) and "бронь подтверждена" in text_of(client_notes[0]),
    )

    own_edit = [
        payload for name, payload in bot.since(mark)
        if name == "EditMessageCaption" and payload.chat_id == ADMIN_ID_2
    ]
    check("У принявшего решение подпись обновлена", bool(own_edit))
    check("В отметке видно «ПОДТВЕРЖДЕНО»", "ПОДТВЕРЖДЕНО" in text_of(own_edit[0]))
    check("В отметке назван администратор", "@second_admin" in text_of(own_edit[0]))
    check("Кнопки у принявшего решение убраны", markup_of(own_edit[0]) is None)

    peer_edit = [
        payload for name, payload in bot.since(mark)
        if name == "EditMessageCaption" and payload.chat_id == ADMIN_ID
    ]
    check("Копия владельца погашена", bool(peer_edit))
    check_equal(
        "Погашена именно та копия, что сохранена в базе",
        peer_edit[0].message_id,
        peer_message_id,
    )
    check("В чужой копии сказано, что заявку обработали", "обработана" in text_of(peer_edit[0]))
    check("В чужой копии названо действие", "подтвердил" in text_of(peer_edit[0]))
    check("В чужой копии назван принявший решение", "@second_admin" in text_of(peer_edit[0]))
    check("В чужой копии убраны кнопки", markup_of(peer_edit[0]) is None)
    check(
        "Повторная рассылка решения не трогает принявшего",
        await sync_peer_admins(bot, db, cfg, decided, exclude=ADMIN_ID_2) == [],
    )

    # --- гонка: два администратора жмут одновременно ------------------------- #
    race = await db.add_booking(MULTI_CLIENT_ID, "Double", "На ночь (22:00–08:00)", "Гонщик", "AgAC_race")
    await db.add_notification(race.id, ADMIN_ID, 5001)
    await db.add_notification(race.id, ADMIN_ID_2, 5002)
    race_bot = BotSpy()
    race_bot.profiles.update(bot.profiles)
    await asyncio.gather(
        admin_handlers.cb_decision(
            make_callback(
                inline_keyboards.admin_approve_callback(race.id),
                user_id=ADMIN_ID,
                bot=race_bot,
                with_photo=True,
                caption="🔔",
            ),
            db,
            race_bot,
            cfg,
        ),
        admin_handlers.cb_decision(
            make_callback(
                inline_keyboards.admin_reject_callback(race.id),
                user_id=ADMIN_ID_2,
                bot=race_bot,
                with_photo=True,
                caption="🔔",
            ),
            db,
            race_bot,
            cfg,
        ),
    )
    final = await db.get_booking(race.id)
    check(
        "При одновременном нажатии статус меняется один раз",
        final.status in (BookingStatus.APPROVED, BookingStatus.REJECTED),
    )
    check("Решение записано за одним администратором", final.decided_by in (ADMIN_ID, ADMIN_ID_2))
    check(
        "Клиенту при одновременном нажатии ушло одно решение",
        len([p for p in race_bot.select("SendMessage") if p.chat_id == MULTI_CLIENT_ID]) == 1,
    )
    race_alerts = [
        payload for name, payload in race_bot.calls
        if name == "AnswerCallbackQuery" and getattr(payload, "show_alert", False)
    ]
    check("Проигравший администратор получил alert «уже обработана»", bool(race_alerts))
    check(
        "В alert назван тот, кто успел первым",
        any("Решение принял" in text_of(payload) for payload in race_alerts),
    )

    # --- панель: список администраторов -------------------------------------- #
    panel_bot = BotSpy()
    panel_bot.profiles[ADMIN_ID_3] = SimpleNamespace(
        id=ADMIN_ID_3, username="new_admin", full_name="Новый"
    )
    panel_bot.profiles.update(bot.profiles)
    mark = panel_bot.mark()
    await admin_handlers.cmd_admins(
        make_message(user_id=ADMIN_ID, bot=panel_bot, text="/admins", username="admin", first_name="Админ"),
        db,
        panel_bot,
        cfg,
    )
    listed = panel_bot.new_messages(mark)[0]
    check("Список администраторов отрисован", "Администраторы клуба" in text_of(listed))
    check("В списке показаны роли", "владелец" in text_of(listed))
    check("В списке показан статус активности", "🟢 работает" in text_of(listed))
    check(
        "Владельцу доступны кнопки управления",
        any(b.text == "➕ Добавить" for row in markup_of(listed).inline_keyboard for b in row),
    )
    check(
        "Владелец из .env защищён — под ним нет кнопок",
        not any(
            b.callback_data and str(ADMIN_ID) in b.callback_data
            for row in markup_of(listed).inline_keyboard
            for b in row
        ),
    )

    # --- панель: добавление администратора ----------------------------------- #
    state = make_fsm_context(ADMIN_ID)
    mark = panel_bot.mark()
    await admin_handlers.cb_admin_add_start(
        make_callback(inline_keyboards.CB_ADMIN_ADD, user_id=ADMIN_ID, bot=panel_bot),
        db,
        panel_bot,
        state,
        admins,
    )
    check_equal("Включён режим ожидания ID", await state.get_state(), AdminFSM.waiting_admin_id)
    check(
        "Объяснено, какой ID прислать",
        any("числовой Telegram ID" in t for t in panel_bot.step_texts(mark)),
    )
    check(
        "Под запросом есть кнопка отмены",
        any(
            b.callback_data == inline_keyboards.CB_ADMIN_ADD_CANCEL
            for row in markup_of(panel_bot.step_payloads(mark)[-1]).inline_keyboard
            for b in row
        ),
    )

    mark = panel_bot.mark()
    await admin_handlers.msg_admin_id(
        make_message(user_id=ADMIN_ID, bot=panel_bot, text="@nickname"), state, db, panel_bot, cfg, admins
    )
    check(
        "Вместо ID — понятная подсказка",
        any("не похоже на числовой Telegram ID" in t for t in panel_bot.new_texts(mark)),
    )
    check_equal("Режим ввода не сброшен после ошибки", await state.get_state(), AdminFSM.waiting_admin_id)
    check("Посторонний не добавился в базу", await db.get_admin(ADMIN_ID_3) is None)

    mark = panel_bot.mark()
    await admin_handlers.msg_admin_id(
        make_message(user_id=ADMIN_ID, bot=panel_bot, text=str(ADMIN_ID_3)), state, db, panel_bot, cfg, admins
    )
    added = await db.get_admin(ADMIN_ID_3)
    check("Новый администратор записан в базу", added is not None and added.is_active)
    check_equal("Новый администратор — не владелец", added.role, AdminRole.OPERATOR)
    check_equal("Записано, кто добавил", added.added_by, ADMIN_ID)
    check_equal("Режим ввода сброшен", await state.get_state(), None)
    check("Реестр сразу знает нового администратора", await admins.is_admin(ADMIN_ID_3))
    check(
        "Предупредили, что новичку нужно нажать /start",
        any("/start" in t for t in panel_bot.new_texts(mark)),
    )
    check(
        "После добавления показан обновлённый список",
        any("Администраторы клуба" in t for t in panel_bot.new_texts(mark)),
    )

    # --- новый администратор получает доступ --------------------------------- #
    check(
        "Добавленный проходит фильтр IsAdmin",
        await IsAdmin()(_event(ADMIN_ID_3), admins=admins, cfg=cfg, db=db),
    )
    mark = panel_bot.mark()
    await admin_handlers.cmd_admin(
        make_message(user_id=ADMIN_ID_3, bot=panel_bot, text="/admin", username="new_admin", first_name="Новый"),
        db,
    )
    check("Новому администратору доступна статистика", any("Всего заявок" in t for t in panel_bot.new_texts(mark)))

    # --- права: оператор не управляет списком -------------------------------- #
    operator_state = make_fsm_context(ADMIN_ID_3)
    mark = panel_bot.mark()
    await admin_handlers.cb_admin_add_start(
        make_callback(inline_keyboards.CB_ADMIN_ADD, user_id=ADMIN_ID_3, bot=panel_bot),
        db,
        panel_bot,
        operator_state,
        admins,
    )
    denies = [payload for name, payload in panel_bot.since(mark) if name == "AnswerCallbackQuery"]
    check("Оператору запрещено управлять списком", bool(denies) and denies[0].show_alert is True)
    check("В отказе сказано про владельца", "только владелец" in text_of(denies[0]))
    check_equal("Режим ввода оператору не включили", await operator_state.get_state(), None)

    mark = panel_bot.mark()
    await admin_handlers.cmd_admins(
        make_message(user_id=ADMIN_ID_3, bot=panel_bot, text="/admins", username="new_admin", first_name="Новый"),
        db,
        panel_bot,
        cfg,
    )
    operator_view = panel_bot.new_messages(mark)[0]
    check("Оператор видит список администраторов", "Администраторы клуба" in text_of(operator_view))
    check("Оператору объяснили отсутствие прав", "только владелец" in text_of(operator_view))
    check(
        "У оператора нет кнопок управления",
        not any(
            b.callback_data and b.callback_data.startswith("ad:admin:")
            for row in markup_of(operator_view).inline_keyboard
            for b in row
        ),
    )

    # --- включение/отключение ------------------------------------------------ #
    mark = panel_bot.mark()
    await admin_handlers.cb_admin_toggle(
        make_callback(inline_keyboards.admin_toggle_callback(ADMIN_ID_3), user_id=ADMIN_ID, bot=panel_bot),
        db,
        panel_bot,
        cfg,
        admins,
    )
    check("Администратор отключён кнопкой", not (await db.get_admin(ADMIN_ID_3)).is_active)
    check("Отключённый потерял доступ", not await admins.is_admin(ADMIN_ID_3))
    check(
        "В списке показан статус «отключён»",
        any("⚪️ отключён" in t for t in panel_bot.step_texts(mark)),
    )
    check("Об отключении сообщили", any("отключён" in text_of(p) for p in panel_bot.answers()[-1:]))

    await admin_handlers.cb_admin_toggle(
        make_callback(inline_keyboards.admin_toggle_callback(ADMIN_ID_3), user_id=ADMIN_ID, bot=panel_bot),
        db,
        panel_bot,
        cfg,
        admins,
    )
    check("Администратор включён обратно", (await db.get_admin(ADMIN_ID_3)).is_active)
    check("Включённый снова проходит фильтр", await admins.is_admin(ADMIN_ID_3))

    # --- смена роли ---------------------------------------------------------- #
    await admin_handlers.cb_admin_role(
        make_callback(inline_keyboards.admin_promote_callback(ADMIN_ID_3), user_id=ADMIN_ID, bot=panel_bot),
        db,
        panel_bot,
        cfg,
        admins,
    )
    check_equal("Роль повышена до владельца", (await db.get_admin(ADMIN_ID_3)).role, AdminRole.OWNER)
    check("Новый владелец получил право управления", await admins.is_owner(ADMIN_ID_3))

    # --- защиты -------------------------------------------------------------- #
    mark = panel_bot.mark()
    await admin_handlers.cb_admin_delete(
        make_callback(inline_keyboards.admin_delete_callback(ADMIN_ID), user_id=ADMIN_ID, bot=panel_bot),
        db,
        panel_bot,
        cfg,
        admins,
    )
    protect = [payload for name, payload in panel_bot.since(mark) if name == "AnswerCallbackQuery"]
    check("Владельца из .env нельзя удалить", bool(protect) and "нельзя" in text_of(protect[0]))
    check("Владелец из .env остался в базе", (await db.get_admin(ADMIN_ID)) is not None)

    mark = panel_bot.mark()
    await admin_handlers.cb_admin_delete(
        make_callback(inline_keyboards.admin_delete_callback(ADMIN_ID_3), user_id=ADMIN_ID_3, bot=panel_bot),
        db,
        panel_bot,
        cfg,
        admins,
    )
    self_protect = [payload for name, payload in panel_bot.since(mark) if name == "AnswerCallbackQuery"]
    check("Себя удалить нельзя", bool(self_protect) and "Себя" in text_of(self_protect[0]))
    check("Самоудаление не тронуло базу", (await db.get_admin(ADMIN_ID_3)) is not None)

    await db.set_admin_active(ADMIN_ID, False)  # как если бы базу правили вручную
    admins.forget(ADMIN_ID)
    mark = panel_bot.mark()
    await admin_handlers.cb_admin_role(
        make_callback(inline_keyboards.admin_demote_callback(ADMIN_ID_3), user_id=ADMIN_ID_3, bot=panel_bot),
        db,
        panel_bot,
        cfg,
        admins,
    )
    last_owner = [payload for name, payload in panel_bot.since(mark) if name == "AnswerCallbackQuery"]
    check(
        "Последнего активного владельца нельзя понизить",
        bool(last_owner) and "последнего" in text_of(last_owner[0]),
    )
    check_equal("Роль последнего владельца не изменилась", (await db.get_admin(ADMIN_ID_3)).role, AdminRole.OWNER)
    await db.set_admin_active(ADMIN_ID, True)
    admins.remember(ADMIN_ID)

    await admin_handlers.cb_admin_role(
        make_callback(inline_keyboards.admin_demote_callback(ADMIN_ID_3), user_id=ADMIN_ID, bot=panel_bot),
        db,
        panel_bot,
        cfg,
        admins,
    )
    check_equal("Роль понижена, когда владельцев двое", (await db.get_admin(ADMIN_ID_3)).role, AdminRole.OPERATOR)

    # --- отмена ввода ID ----------------------------------------------------- #
    cancel_state = make_fsm_context(ADMIN_ID)
    await cancel_state.set_state(AdminFSM.waiting_admin_id)
    mark = panel_bot.mark()
    await admin_handlers.cb_admin_add_cancel(
        make_callback(inline_keyboards.CB_ADMIN_ADD_CANCEL, user_id=ADMIN_ID, bot=panel_bot),
        cancel_state,
        panel_bot,
    )
    check_equal("Отмена сбрасывает режим ввода", await cancel_state.get_state(), None)
    check("Об отмене сообщили", any("отменено" in t for t in panel_bot.step_texts(mark)))

    # --- удаление ------------------------------------------------------------ #
    mark = panel_bot.mark()
    await admin_handlers.cb_admin_delete(
        make_callback(inline_keyboards.admin_delete_callback(ADMIN_ID_3), user_id=ADMIN_ID, bot=panel_bot),
        db,
        panel_bot,
        cfg,
        admins,
    )
    check("Администратор удалён из базы", (await db.get_admin(ADMIN_ID_3)) is None)
    check("Удалённый потерял доступ", not await admins.is_admin(ADMIN_ID_3))
    check("Об удалении сообщили", any("удалён" in text_of(p) for p in panel_bot.answers()[-1:]))

    # --- статистика и приветствие -------------------------------------------- #
    stats = await db.get_stats()
    check("Статистика считает администраторов", stats.admins == 2 and stats.admins_active == 2)
    mark = panel_bot.mark()
    await admin_handlers.cmd_admin(
        make_message(user_id=ADMIN_ID, bot=panel_bot, text="/admin", username="admin", first_name="Админ"),
        db,
    )
    panel_payload = panel_bot.new_messages(mark)[0]
    check("В статистике панели видно администраторов", "Администраторов" in text_of(panel_payload))
    check(
        "В панели есть кнопка «👥 Администраторы»",
        any(b.text == "👥 Администраторы" for row in markup_of(panel_payload).inline_keyboard for b in row),
    )

    mark = panel_bot.mark()
    admin_state = make_fsm_context(ADMIN_ID_2)
    await db.upsert_user(ADMIN_ID_2, "second_admin", "Второй")
    await db.set_consent(ADMIN_ID_2, granted=True)
    await client_handlers.cmd_start(
        make_message(user_id=ADMIN_ID_2, bot=panel_bot, text="/start", username="second_admin", first_name="Второй"),
        admin_state,
        db,
        cfg,
        admins,
    )
    greeting = panel_bot.new_texts(mark)
    check(
        "Администратору /start показывает служебную подсказку",
        any("Вы администратор этого бота" in t for t in greeting),
    )
    check("В подсказке названа роль", any("администратор" in t for t in greeting))
    check("В подсказке упомянута команда /admins", any("/admins" in t for t in greeting))

    mark = panel_bot.mark()
    client_state = make_fsm_context(MULTI_CLIENT_ID)
    await client_handlers.cmd_start(
        make_message(
            user_id=MULTI_CLIENT_ID,
            bot=panel_bot,
            text="/start",
            username="multi_client",
            first_name="Мультиклиент",
        ),
        client_state,
        db,
        cfg,
        admins,
    )
    check(
        "Клиенту служебная подсказка администратора не показывается",
        not any("Вы администратор этого бота" in t for t in panel_bot.new_texts(mark)),
    )

    # --- команды панели без прав --------------------------------------------- #
    mark = panel_bot.mark()
    await client_handlers.cmd_admin_no_access(
        make_message(
            user_id=MULTI_CLIENT_ID,
            bot=panel_bot,
            text="/admin",
            username="multi_client",
            first_name="Мультиклиент",
        ),
        db,
    )
    check(
        "Клиенту на /admin объясняют, что команда для администраторов",
        any("доступна администраторам" in t for t in panel_bot.new_texts(mark)),
    )

    await db.set_admin_active(ADMIN_ID_2, False)
    mark = panel_bot.mark()
    await client_handlers.cmd_admin_no_access(
        make_message(
            user_id=ADMIN_ID_2, bot=panel_bot, text="/admin", username="second_admin", first_name="Второй"
        ),
        db,
    )
    check(
        "Отключённому администратору сообщают об отключении",
        any("отключена владельцем" in t for t in panel_bot.new_texts(mark)),
    )
    await db.set_admin_active(ADMIN_ID_2, True)
    admins.remember(ADMIN_ID_2)

    for context in (state, operator_state, cancel_state, admin_state, client_state):
        await context.storage.close()


# --------------------------------------------------------------------------- #
# 12. Обновление базы, созданной предыдущей версией бота
# --------------------------------------------------------------------------- #
OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    consent_given BOOLEAN NOT NULL DEFAULT 0,
    created_at    DATETIME
);
CREATE TABLE IF NOT EXISTS bookings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    tariff          TEXT    NOT NULL,
    time_slot       TEXT    NOT NULL,
    client_name     TEXT    NOT NULL,
    receipt_file_id TEXT,
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at      DATETIME
);
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings (status, id);
"""


async def test_migration() -> None:
    print("\n— Обновление старой базы данных —")
    workdir = Path(tempfile.mkdtemp(prefix="admiral-migrate-"))
    old_path = workdir / "old.db"
    db = Database(old_path, timezone=TEST_TZ)
    try:
        connection = sqlite3.connect(old_path)
        try:
            connection.executescript(OLD_SCHEMA)
            connection.execute(
                "INSERT INTO users (user_id, username, first_name, consent_given, created_at) "
                "VALUES (1, 'old_client', 'Старый', 1, '2026-01-01T10:00:00+11:00')"
            )
            connection.execute(
                "INSERT INTO bookings (user_id, tariff, time_slot, client_name, receipt_file_id, "
                "status, created_at) VALUES (1, 'VIP', 'Сейчас (с 10:00)', 'Старый', 'AgAC_old', "
                "'pending', '2026-01-01T10:05:00+11:00')"
            )
            connection.commit()
        finally:
            connection.close()

        await db.connect()
        tables = await db.list_tables()
        check("В старую базу добавлена таблица admins", "admins" in tables)
        check("В старую базу добавлена таблица booking_notifications", "booking_notifications" in tables)
        check("Старые таблицы сохранены", {"users", "bookings"} <= set(tables))

        booking = await db.get_booking(1)
        check(
            "Старая заявка читается после миграции",
            booking is not None and booking.client_name == "Старый",
        )
        check(
            "Новые столбцы у старой заявки пустые",
            booking.decided_by is None and booking.decided_at is None,
        )
        check("Старые данные не потеряны", (await db.get_stats()).users == 1)

        check(
            "Статус старой заявки меняется и пишет, кто решил",
            await db.set_status_if_pending(1, BookingStatus.APPROVED, decided_by=42),
        )
        updated = await db.get_booking(1)
        check_equal("Кто принял решение — записан", updated.decided_by, 42)
        check("Когда принято решение — записано", bool(updated.decided_at))
        stats = await db.get_stats()
        check(
            "Статистика старой базы считается",
            stats.total == 1 and stats.approved == 1 and stats.admins == 0,
        )
    finally:
        await db.close()
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #
async def run_all() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="admiral-db-"))
    db_path = workdir / "test.db"
    cfg = make_cfg(db_path)
    db: Database | None = None

    try:
        test_config()

        db = await init_database(db_path, TEST_TZ)
        # Как в main.py: администраторы из .env попадают в таблицу admins.
        await AdminRegistry(db, cfg).startup()
        await test_database(db)

        # Повторное подключение не должно ломать существующую базу.
        await db.close()
        db = await init_database(db_path, TEST_TZ)
        check("Схема применяется повторно без потери данных", (await db.get_stats()).users >= 1)

        test_booking_logic()
        test_keyboards()
        test_texts()
        await test_full_scenario(db, cfg, SCENARIO_CLIENT_ID)
        await test_cancel_and_outdated(db, cfg)
        await test_edit_fallback(db, cfg)
        admins = AdminRegistry(db, cfg)
        await admins.startup()
        dispatcher = build_dispatcher(db, cfg, admins)
        await test_wiring(db, cfg, dispatcher)
        await test_dispatcher_routing(db, cfg, dispatcher)
        await dispatcher.storage.close()

        await test_multi_admin(db)
        await test_migration()
    finally:
        await db.close()
        shutil.rmtree(workdir, ignore_errors=True)

    failed = [name for name, ok, _ in _results if not ok]
    print("\n" + "=" * 64)
    print(
        f"Проверок выполнено: {len(_results)}, "
        f"успешно: {len(_results) - len(failed)}, ошибок: {len(failed)}"
    )
    if failed:
        print("Провалены:")
        for name in failed:
            print(f"  • {name}")
        print("=" * 64)
        return 1
    print("Все проверки пройдены ✅")
    print("=" * 64)
    return 0


def main() -> int:
    return asyncio.run(run_all())


if __name__ == "__main__":
    sys.exit(main())
