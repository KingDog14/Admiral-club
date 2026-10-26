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
from datetime import date, datetime
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
from bot.config import AppConfig, ClubInfo, ConfigError, load_config  # noqa: E402
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
from bot.states import (  # noqa: E402
    DATA_CLIENT_NAME,
    DATA_TARIFF_KEY,
    DATA_TIME_KEY,
    DATA_TIME_SLOT,
    AdminFSM,
    BookingFSM,
)

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

    def _sent_message(
        self, kwargs: dict[str, Any], *, with_photo: bool = False
    ) -> SimpleNamespace:
        """Подставить результат отправки: бот использует ``message.message_id`` и ``photo``."""
        self._message_ids += 1
        photos = (
            [SimpleNamespace(file_id=f"AgAC_card_{self._message_ids}")]
            if with_photo
            else None
        )
        return SimpleNamespace(
            message_id=self._message_ids,
            chat=SimpleNamespace(id=kwargs.get("chat_id")),
            photo=photos,
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
        return self._sent_message(kwargs, with_photo=True)

    async def get_chat(self, chat_id: int, **kwargs: Any) -> Any:
        """Профиль пользователя — нужен, если администратора нет в таблице users."""
        self.calls.append(("GetChat", SimpleNamespace(chat_id=chat_id, **kwargs)))
        if chat_id in self.profiles:
            return self.profiles[chat_id]
        return SimpleNamespace(
            id=chat_id, username=None, full_name=f"Пользователь {chat_id}"
        )

    async def send_contact(self, **kwargs: Any) -> None:
        self.calls.append(("SendContact", SimpleNamespace(**kwargs)))

    async def edit_message_text(self, **kwargs: Any) -> None:
        self.calls.append(("EditMessageText", SimpleNamespace(**kwargs)))

    async def edit_message_caption(self, **kwargs: Any) -> None:
        self.calls.append(("EditMessageCaption", SimpleNamespace(**kwargs)))

    async def edit_message_media(self, **kwargs: Any) -> Any:
        self.calls.append(("EditMessageMedia", SimpleNamespace(**kwargs)))
        return self._sent_message(kwargs, with_photo=True)

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
            if name
            in (
                "SendMessage",
                "SendPhoto",
                "EditMessageText",
                "EditMessageCaption",
                "EditMessageMedia",
            )
        ]

    def step_texts(self, mark: int) -> list[str]:
        """Все тексты шага: и новые сообщения, и правки существующих."""
        return [text_of(payload) for payload in self.step_payloads(mark)]


async def _noop() -> None:
    return None


def text_of(payload: Any) -> str:
    """Текст сообщения или подпись к фото/медиа из записанного вызова."""
    media = getattr(payload, "media", None)
    media_caption = getattr(media, "caption", None) if media is not None else None
    return str(
        getattr(payload, "text", None)
        or getattr(payload, "caption", None)
        or media_caption
        or ""
    )


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
            PhotoSize(
                file_id=f"AgAC_size_{width}",
                file_unique_id=f"u{width}",
                width=width,
                height=width,
            )
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
            check(
                "Пустой .env должен вызывать ConfigError",
                False,
                "исключение не поднято",
            )
        except ConfigError as exc:
            check(
                "Пустой .env → понятная ошибка про BOT_TOKEN", "BOT_TOKEN" in str(exc)
            )

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
        check(
            "Путь к базе по умолчанию — внутри проекта",
            cfg.db_path.name == "nova_demo.db",
        )

        _clear_env()
        bad_admin = env_dir / "bad.env"
        bad_admin.write_text(
            f"BOT_TOKEN={TEST_TOKEN}\nADMIN_ID=@nickname\n", encoding="utf-8"
        )
        try:
            load_config(bad_admin)
            check(
                "ADMIN_ID=@nickname должен отклоняться", False, "исключение не поднято"
            )
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
        check(
            "Первый ID распознаётся как администратор", cfg_multi.is_env_admin(ADMIN_ID)
        )
        check(
            "Второй ID распознаётся как администратор",
            cfg_multi.is_env_admin(ADMIN_ID_2),
        )
        check(
            "Посторонний ID не администратор", not cfg_multi.is_env_admin(DB_CLIENT_ID)
        )

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
            f"BOT_TOKEN={TEST_TOKEN}\nADMIN_IDS={ADMIN_ID},@nickname\n",
            encoding="utf-8",
        )
        try:
            load_config(bad_multi)
            check(
                "ADMIN_IDS с @nickname должен отклоняться",
                False,
                "исключение не поднято",
            )
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
            check(
                "Без ADMIN_IDS запуск должен отклоняться",
                False,
                "исключение не поднято",
            )
        except ConfigError as exc:
            check(
                "Без ADMIN_IDS ошибка объясняет, что вписать", "ADMIN_IDS" in str(exc)
            )

        _clear_env()
        bad_tz = env_dir / "bad_tz.env"
        bad_tz.write_text(
            f"BOT_TOKEN={TEST_TOKEN}\nADMIN_ID={ADMIN_ID}\nTZ=Mars/Olympus\n",
            encoding="utf-8",
        )
        try:
            load_config(bad_tz)
            check(
                "Неизвестный часовой пояс должен отклоняться",
                False,
                "исключение не поднято",
            )
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
    check(
        "Схема создана: users и bookings",
        {"users", "bookings"}.issubset(set(tables)),
        str(tables),
    )

    user = await db.upsert_user(DB_CLIENT_ID, "ivan_petrov", "Иван")
    check_equal("Пользователь создан", user.user_id, DB_CLIENT_ID)
    check("Согласие по умолчанию не дано (152-ФЗ)", user.consent_given is False)
    check(
        "has_consent = False до нажатия кнопки",
        (await db.has_consent(DB_CLIENT_ID)) is False,
    )

    await db.set_consent(DB_CLIENT_ID, granted=True)
    check("Согласие зафиксировано", (await db.has_consent(DB_CLIENT_ID)) is True)

    await db.upsert_user(DB_CLIENT_ID, "ivan_new", "Иван Петров")
    refreshed = await db.get_user(DB_CLIENT_ID)
    check(
        "Профиль обновляется",
        refreshed.username == "ivan_new" and refreshed.first_name == "Иван Петров",
    )
    check(
        "Согласие не теряется при обновлении профиля", refreshed.consent_given is True
    )

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
    check_equal(
        "Подсчёт pending-заявок", await db.count_bookings(BookingStatus.PENDING), 7
    )

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
    check(
        "as_dict() пригоден для format(**stats)",
        set(stats.as_dict()) >= {"total", "pending"},
    )

    history = await db.list_user_bookings(DB_CLIENT_ID, limit=3)
    check_equal("История заявок клиента ограничена limit", len(history), 3)

    created = await asyncio.gather(
        *(
            db.add_booking(
                DB_CLIENT_ID, "Standart", "Сейчас", f"Поток {i}", f"AgAC_c{i}"
            )
            for i in range(5)
        )
    )
    check(
        "5 параллельных записей прошли без блокировки",
        len({b.id for b in created}) == 5,
    )

    try:
        await db.add_booking(
            DB_CLIENT_ID, "VIP", "Сейчас", "Тест", "x", status="unknown"
        )
        check("Недопустимый статус должен отклоняться", False, "исключение не поднято")
    except ValueError:
        check("Недопустимый статус брони отклоняется", True)


# --------------------------------------------------------------------------- #
# 3. Логика бронирования
# --------------------------------------------------------------------------- #
def test_booking_logic() -> None:
    print("\n— Логика бронирования —")
    moment = datetime(2026, 10, 23, 17, 51, tzinfo=TEST_TZ)

    check_equal(
        "«Сейчас» фиксирует время обращения",
        time_slot_text("now", moment),
        "Сейчас (с 17:51, 23.10.2026)",
    )
    check_equal(
        "«На ночь» → ночной пакет",
        time_slot_text("night", moment),
        "Ночь (22:00–08:00)",
    )
    check_equal(
        "«Утро» → утренний пакет",
        time_slot_text("morning", moment),
        "Утро (8:00–13:00)",
    )
    check_equal(
        "«День» → дневной пакет", time_slot_text("day", moment), "День (13:00–17:00)"
    )
    check("«Другое» требует ввода текстом", time_slot_text("other", moment) is None)
    check(
        "Неизвестный вариант времени не принимается",
        time_slot_text("когда-нибудь", moment) is None,
    )

    check_equal(
        "Имя приводится к аккуратному виду", clean_client_name("  иван  "), "Иван"
    )
    check_equal("Имя с дефисом проходит", clean_client_name("анна-мария"), "Анна-мария")
    check("Имя из одной буквы отклоняется", clean_client_name("И") is None)
    check("Имя длиннее 40 символов отклоняется", clean_client_name("И" * 41) is None)
    check("Команда вместо имени отклоняется", clean_client_name("/start") is None)
    check(
        "Кнопка меню вместо имени отклоняется",
        clean_client_name("🎮 Забронировать") is None,
    )
    check(
        "Ссылка вместо имени отклоняется", clean_client_name("http://spam.ru") is None
    )
    check("Имя с цифрами отклоняется", clean_client_name("Иван2") is None)

    check_equal(
        "Своё время сохраняется текстом",
        clean_custom_time(" сегодня в 18:30 на 3 часа "),
        "сегодня в 18:30 на 3 часа",
    )
    check("Время без цифр отклоняется", clean_custom_time("когда-нибудь") is None)
    check(
        "Время длиннее 60 символов отклоняется",
        clean_custom_time("в 18:00 " + "а" * 60) is None,
    )
    check("Команда вместо времени отклоняется", clean_custom_time("/cancel") is None)

    check(
        "Нажатие кнопки меню распознаётся",
        is_menu_button_text(reply_keyboards.MENU_PRICE) is True,
    )
    check(
        "Кнопка «Частые вопросы» в меню",
        is_menu_button_text(reply_keyboards.MENU_FAQ) is True,
    )
    check(
        "Обычный текст кнопкой меню не считается", is_menu_button_text("Иван") is False
    )

    # --- реальные цены клуба: суббота против вторника ----------------------- #
    saturday, tuesday = date(2026, 10, 17), date(2026, 10, 20)
    check("17.10.2026 — суббота (выходной)", texts.is_weekend(saturday) is True)
    check("20.10.2026 — вторник (будни)", texts.is_weekend(tuesday) is False)
    check_equal(
        "Ночь Standart в субботу — 650 ₽ (демо-прайс)",
        texts.amount_due("standart", "night", saturday),
        650,
    )
    check_equal(
        "Ночь Standart в будни — 550 ₽",
        texts.amount_due("standart", "night", tuesday),
        550,
    )
    check_equal(
        "4 ночных места Standart в субботу — 2600 ₽",
        texts.amount_due("standart", "night", saturday) * 4,
        2600,
    )
    check_equal(
        "Ночь Double в субботу — 1300 ₽",
        texts.amount_due("double", "night", saturday),
        1300,
    )
    check_equal("Утро VIP — 480 ₽", texts.amount_due("vip", "morning", saturday), 480)
    check_equal("День S VIP — 800 ₽", texts.amount_due("svip", "day", saturday), 800)
    check(
        "Почасовая игра считается по прайсу",
        texts.amount_due("vip", "now", saturday) is None,
    )
    check(
        "Своё время считается по прайсу",
        texts.amount_due("vip", "other", saturday) is None,
    )

    check(
        "Ночь в субботу требует предоплаты",
        texts.prepayment_required("night", saturday) is True,
    )
    check(
        "Ночь во вторник — без предоплаты",
        texts.prepayment_required("night", tuesday) is False,
    )
    check(
        "Утро в субботу — без предоплаты",
        texts.prepayment_required("morning", saturday) is False,
    )
    check(
        "День в субботу — без предоплаты",
        texts.prepayment_required("day", saturday) is False,
    )
    check(
        "«Сейчас» — без предоплаты", texts.prepayment_required("now", saturday) is False
    )

    check_equal(
        "Сумма к оплате для ночи в субботу",
        texts.amount_text("standart", "night", saturday),
        "<b>650 ₽</b>",
    )
    check(
        "Сумма для почасовой игры — «по прайсу»",
        "по прайсу" in texts.amount_text("standart", "now", saturday),
    )


# --------------------------------------------------------------------------- #
# 4. Клавиатуры и callback_data
# --------------------------------------------------------------------------- #
def test_keyboards() -> None:
    print("\n— Клавиатуры —")
    from bot.ui.screens import Screen, ScreenCB, faq_list_keyboard, menu_keyboard, nav

    guest_menu = menu_keyboard(is_admin=False)
    guest_buttons = [b.text for row in guest_menu.inline_keyboard for b in row]
    check_equal("Главное меню гостя: 7 inline-кнопок", len(guest_buttons), 7)
    check(
        "У гостя нет кнопки «🛠 Админ-панель»",
        all("Админ-панель" not in t for t in guest_buttons),
    )
    admin_menu = menu_keyboard(is_admin=True)
    admin_buttons = [b.text for row in admin_menu.inline_keyboard for b in row]
    check_equal("Главное меню администратора: 8 inline-кнопок", len(admin_buttons), 8)
    check(
        "У администратора есть кнопка «🛠 Админ-панель»",
        "🛠 Админ-панель" in admin_buttons,
    )

    faq_kb = faq_list_keyboard()
    faq_buttons = [b for row in faq_kb.inline_keyboard for b in row]
    check_equal(
        "В FAQ по кнопке на каждый вопрос + Назад/В меню",
        len(faq_buttons),
        len(texts.FAQ_ITEMS) + 2,
    )
    check(
        "Первый вопрос FAQ распаковывается через ScreenCB",
        ScreenCB.unpack(faq_buttons[0].callback_data).screen == Screen.FAQ_ITEM,
    )
    check(
        "Под FAQ есть «◀ Назад» и «🏠 В меню»",
        [b.text for b in faq_kb.inline_keyboard[-1]] == ["◀ Назад", "🏠 В меню"],
    )

    legacy_menu = reply_keyboards.main_menu()
    check(
        "Запасное reply-меню уменьшенное (resize_keyboard)",
        legacy_menu.resize_keyboard is True,
    )
    _ = nav

    consent = inline_keyboards.consent_keyboard()
    check_equal("Экран согласия: одна кнопка", len(consent.inline_keyboard[0]), 1)
    check_equal(
        "Текст кнопки согласия", consent.inline_keyboard[0][0].text, "✅ Я согласен"
    )

    privacy = inline_keyboards.privacy_keyboard()
    check_equal(
        "В /privacy есть кнопка отзыва согласия", len(privacy.inline_keyboard[0]), 1
    )
    check_equal(
        "Текст кнопки отзыва согласия",
        privacy.inline_keyboard[0][0].text,
        "🚫 Отозвать согласие",
    )
    check_equal(
        "Кнопка отзыва ведёт на consent:revoke",
        privacy.inline_keyboard[0][0].callback_data,
        inline_keyboards.CB_CONSENT_REVOKE,
    )
    revoke = inline_keyboards.revoke_confirm_keyboard()
    revoke_buttons = [b for row in revoke.inline_keyboard for b in row]
    check_equal("Подтверждение отзыва: две кнопки", len(revoke_buttons), 2)
    check_equal(
        "Подтверждение отзыва: правильные callback",
        [b.callback_data for b in revoke_buttons],
        [inline_keyboards.CB_CONSENT_REVOKE_YES, inline_keyboards.CB_CONSENT_REVOKE_NO],
    )

    pay_prepaid = inline_keyboards.payment_choice_keyboard(True)
    pay_prepaid_buttons = [b for row in pay_prepaid.inline_keyboard for b in row]
    check_equal("Шаг 4 с предоплатой: чек + отмена", len(pay_prepaid_buttons), 2)
    check(
        "При обязательной предоплате нет кнопки «на месте»",
        all("на месте" not in b.text for b in pay_prepaid_buttons),
    )
    pay_free = inline_keyboards.payment_choice_keyboard(False)
    pay_free_buttons = [b for row in pay_free.inline_keyboard for b in row]
    check_equal("Шаг 4 без предоплаты: чек, на месте, отмена", len(pay_free_buttons), 3)
    check_equal(
        "Разбор способа оплаты: чек",
        inline_keyboards.parse_payment_way(inline_keyboards.CB_PAY_CHECK),
        inline_keyboards.PAY_WAY_CHECK,
    )
    check_equal(
        "Разбор способа оплаты: на месте",
        inline_keyboards.parse_payment_way(inline_keyboards.CB_PAY_SPOT),
        inline_keyboards.PAY_WAY_SPOT,
    )
    check(
        "Мусор в способе оплаты отклоняется",
        inline_keyboards.parse_payment_way("bk:pay:xxx") is None,
    )
    check(
        "Чужой префикс не считается оплатой",
        inline_keyboards.parse_payment_way("bk:t:vip") is None,
    )

    tariffs = inline_keyboards.tariffs_keyboard()
    tariff_buttons = [b for row in tariffs.inline_keyboard for b in row]
    check_equal("Шаг 1: 4 тарифа + отмена", len(tariff_buttons), 5)
    check_equal(
        "Тарифы: Standart, VIP, S VIP, Double",
        [texts.TARIFF_TITLE_BY_KEY[key] for key in texts.TARIFF_TITLE_BY_KEY],
        ["Standart", "VIP", "S VIP", "Double"],
    )

    times = inline_keyboards.time_slots_keyboard()
    check_equal(
        "Шаг 2: 5 вариантов времени + отмена",
        len([b for row in times.inline_keyboard for b in row]),
        6,
    )
    check(
        "Варианты времени: сейчас, утро, день, ночь, другое",
        [texts.TIME_TITLE_BY_KEY[key] for key, _ in texts.TIME_CHOICES]
        == [
            "⚡️ Сейчас (почасово)",
            "🌅 Утро (8:00–13:00)",
            "☀️ День (13:00–17:00)",
            "🌙 Ночь (22:00–08:00)",
            "✏️ Другое время",
        ],
    )

    decision = inline_keyboards.booking_decision_keyboard(123)
    check_equal("Под чеком две кнопки", len(decision.inline_keyboard[0]), 2)
    check_equal(
        "Текст кнопки подтверждения",
        decision.inline_keyboard[0][0].text,
        "✅ Подтвердить",
    )
    check_equal(
        "Текст кнопки отклонения", decision.inline_keyboard[0][1].text, "❌ Отклонить"
    )

    pending = inline_keyboards.pending_list_keyboard(
        [SimpleNamespace(id=1), SimpleNamespace(id=2)], page=1, pages=3
    )
    check(
        "Навигация по страницам: назад, вперёд, статистика",
        len(pending.inline_keyboard[-1]) == 3,
    )

    named_keyboards = {
        "меню гостя": guest_menu,
        "меню администратора": admin_menu,
        "FAQ": faq_kb,
        "согласие": consent,
        "/privacy": privacy,
        "подтверждение отзыва": revoke,
        "тарифы": tariffs,
        "время": times,
        "способ оплаты (предоплата)": pay_prepaid,
        "способ оплаты (на месте)": pay_free,
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
        kb_callbacks = [
            b.callback_data
            for row in kb.inline_keyboard
            for b in row
            if b.callback_data
        ]
        check(
            f"Клавиатура «{kb_name}»: callback_data уникальны",
            len(set(kb_callbacks)) == len(kb_callbacks),
            str(kb_callbacks),
        )

    check_equal(
        "Разбор тарифа из callback",
        inline_keyboards.parse_tariff_key("bk:t:svip"),
        "svip",
    )
    check(
        "Чужой тариф не принимается",
        inline_keyboards.parse_tariff_key("bk:t:hacker") is None,
    )
    check(
        "Чужой префикс не принимается",
        inline_keyboards.parse_tariff_key("ad:ok:1") is None,
    )
    check_equal(
        "Разбор времени из callback",
        inline_keyboards.parse_time_key("bk:w:night"),
        "night",
    )
    check_equal(
        "Разбор решения админа",
        inline_keyboards.parse_admin_decision("ad:no:7"),
        (BookingStatus.REJECTED, 7),
    )
    check(
        "Решение с мусором вместо id отклоняется",
        inline_keyboards.parse_admin_decision("ad:ok:abc") is None,
    )
    check_equal(
        "Разбор страницы списка", inline_keyboards.parse_admin_list_page("ad:list:2"), 2
    )


# --------------------------------------------------------------------------- #
# 5. Тексты
# --------------------------------------------------------------------------- #
def test_texts() -> None:
    print("\n— Тексты —")
    club = ClubInfo()
    price = texts.build_price_list(club)
    for title in ("Standart", "VIP", "S VIP", "Double"):
        # Клуб подписывает залы заглавными: «💻 STANDART (12 PC)».
        check(f"Прайс содержит тариф {title}", title.upper() in price)
    check("Прайс содержит ночной пакет", texts.NIGHT_SLOT in price)
    check("Прайс содержит утренний пакет", texts.MORNING_SLOT in price)
    check("Прайс содержит дневной пакет", texts.DAY_SLOT in price)
    check("Прайс содержит реквизиты", club.payment_card in price)
    check("В прайсе не осталось плейсхолдеров", "{" not in price)
    check("Прайс предупреждает про предоплату только в ПТ–ВС", "ПТ–ВС" in price)
    check("Прайс упоминает FACEIT", "FACEIT" in price)

    # Демонстрационные цены из прайса NOVA ARENA — построчно.
    for line in (
        "1 час: 120 ₽ • 3 часа: 320 ₽ • 5 часов: 500 ₽",
        "1 час: 170 ₽ • 3 часа: 460 ₽ • 5 часов: 700 ₽",
        "1 час: 230 ₽ • 3 часа: 620 ₽ • 5 часов: 950 ₽",
        "1 час: 280 ₽ • 3 часа: 750 ₽ • 5 часов: 1150 ₽",
    ):
        check(f"В прайсе есть строка «{line}»", line in price)

    rules = texts.build_rules_text(club)
    check(
        "Правила собраны без плейсхолдеров",
        "{" not in rules and "Бронирование и оплата" in rules,
    )
    check(
        "Правила объясняют предоплату только на ночь в ПТ–ВС",
        "только в выходные (ПТ–ВС)" in rules,
    )
    check("Правила предупреждают о мошенниках", "мошенник" in rules.lower())
    check("Правила называют единый номер клуба", club.payment_card in rules)
    check("Правила просят обсуждать предоплату в группе", "общей группе" in rules)

    contacts = texts.build_contacts_text(club)
    check(
        "Контакты содержат телефон клуба",
        club.admin_phone in contacts and "{" not in contacts,
    )
    check("Контакты содержат адрес клуба", club.address in contacts)
    check("Контакты предупреждают о мошенниках", "мошенник" in contacts.lower())

    faq = texts.build_faq_text(club)
    check("FAQ собран без плейсхолдеров", "{" not in faq)
    check("FAQ отвечает, во сколько начинается ночь", "22:00" in faq and "08:00" in faq)
    check("FAQ объясняет предоплату", "ПТ–ВС" in faq)
    check("FAQ предупреждает о мошенниках", "мошенник" in faq.lower())
    check("FAQ отвечает про FACEIT", "FACEIT" in faq)
    check("FAQ называет единый номер", club.payment_card in faq)

    consent = texts.CONSENT_TEXT
    check("Согласие ссылается на 152-ФЗ", "152-ФЗ" in consent)
    check(
        "Согласие перечисляет состав данных",
        "Telegram ID" in consent and "чека" in consent,
    )
    check("Согласие описывает цель обработки", "Цель обработки" in consent)
    check("Согласие описывает отзыв", "отозв" in consent.lower())

    payment = texts.render(
        texts.PAYMENT_DETAILS,
        club,
        tariff="VIP",
        time_slot="Сейчас",
        client_name="Иван",
        amount=texts.amount_text("vip", "now", date.today()),
        payment_rules=texts.render(texts.PAYMENT_RULES_ON_SPOT, club),
    )
    check(
        "Шаг оплаты содержит демо-реквизиты и номер",
        club.payment_system in payment and club.payment_card in payment,
    )
    check("Шаг оплаты содержит имя получателя", club.payment_holder in payment)
    check(
        "Шаг оплаты предлагает выбрать способ оплаты",
        "способ оплаты" in payment.lower(),
    )
    check("Шаг оплаты без предоплаты разрешает платить на месте", "на месте" in payment)

    prepaid = texts.render(
        texts.PAYMENT_DETAILS,
        club,
        tariff="Standart",
        time_slot="Ночь (22:00–08:00)",
        client_name="Иван",
        amount=texts.amount_text("standart", "night", date(2026, 10, 17)),
        payment_rules=texts.render(texts.PAYMENT_RULES_PREPAID, club),
    )
    check(
        "Ночь в ПТ–ВС: шаг оплаты требует полную предоплату",
        "полной предоплате" in prepaid,
    )
    check("Ночь в ПТ–ВС: указана сумма 650 ₽", "650 ₽" in prepaid)
    check("Ночь в ПТ–ВС: просит фото чека", "фото чека" in prepaid.lower())

    samples = {
        texts.GREETING: {"user_name": "Иван"},
        texts.CHOOSE_TARIFF: {"tariffs_overview": texts.build_tariffs_overview()},
        texts.CHOOSE_TIME: {
            "tariff": "VIP",
            "now_time": "17:51",
            "now_date": "23 октября",
        },
        texts.ENTER_CUSTOM_TIME: {},
        texts.ENTER_NAME: {"tariff": "VIP", "time_slot": "Сейчас"},
        texts.RECEIPT_RECEIVED: {
            "booking_id": 1,
            "tariff": "VIP",
            "time_slot": "Сейчас",
            "client_name": "Иван",
            "amount": "<b>190 ₽</b>",
        },
        texts.RECEIPT_NOT_PHOTO: {},
        texts.RECEIPT_WAITING: {},
        texts.BOOKING_SPOT_RECEIVED: {
            "booking_id": 1,
            "tariff": "VIP",
            "time_slot": "Сейчас",
            "client_name": "Иван",
        },
        texts.RECEIPT_ADMIN_UNAVAILABLE: {"booking_id": 1},
        texts.MENU_HINT: {},
        texts.HELP_TEXT: {},
        texts.PRIVACY_TEXT: {},
        texts.CONSENT_TEXT: {},
        texts.CONSENT_REVOKE_CONFIRM: {},
        texts.CONSENT_REVOKE_CANCELED: {},
        texts.CONSENT_REVOKED: {"revoked_at": "23.10.2026 17:51"},
        texts.ADMIN_WELCOME: {},
        texts.ADMIN_GOODBYE: {},
        texts.ADMIN_ROLE_CHANGED: {
            "role": "владелец",
            "note": texts.ADMIN_ROLE_NOTE_OWNER,
        },
        texts.ADMIN_ROLE_PROMPT: {
            "user_link": "@admin",
            "user_id": 1,
            "role": "администратор",
            "state": texts.ADMINS_STATE_ACTIVE,
        },
        texts.ADMIN_ROLE_PROMPT_OWNER: {"user_link": "@owner", "user_id": 2},
        texts.NEW_BOOKING_CAPTION: {
            "booking_id": 1,
            "tariff": "VIP",
            "time_slot": "Сейчас",
            "client_name": "Иван",
            "user_link": "@client",
            "created_at": "23.10.2026 17:51",
            "amount": "<b>850 ₽</b>",
            "payment_note": texts.NEW_BOOKING_RECEIPT_NOTE,
            "admins_count": 2,
        },
        texts.CLIENT_APPROVED: {
            "tariff": "VIP",
            "time_slot": "Сейчас",
            "booking_id": 1,
            "client_name": "Иван",
        },
        texts.CLIENT_REJECTED: {
            "booking_id": 1,
            "tariff": "VIP",
            "time_slot": "Сейчас",
        },
        texts.STATS_TEMPLATE: {
            "now": "23.10.2026 17:51",
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
        texts.ADMIN_ALREADY_PROCESSED_NOTE: {
            "status": "подтверждена",
            "decider": "@admin",
        },
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
            "stats": texts.STATS_TEMPLATE.format(
                now="23.10.2026 17:51",
                total=10,
                pending=3,
                approved=5,
                rejected=2,
                users=8,
                consented=8,
                admins=2,
                admins_active=2,
            ),
        },
        texts.ADMIN_START_INFO: {"role": "владелец"},
        texts.ADMINS_LIST_HEADER: {"total": 3, "active": 2},
        texts.ADMINS_ITEM: {
            "index": 1,
            "user_link": "@admin",
            "user_id": 111,
            "role": "владелец",
            "state": texts.ADMINS_STATE_ACTIVE,
            "added": "",
        },
        texts.ADMINS_ITEM_ADDED: {"added_by": "@admin"},
        texts.ADMIN_ADD_PROMPT: {},
        texts.ADMIN_ADDED: {
            "user_link": "@new",
            "user_id": 42,
            "role": "администратор",
        },
        texts.ADMIN_ALREADY_EXISTS: {
            "user_link": "@new",
            "user_id": 42,
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
        texts.ADMIN_ADD_INVALID_ID: {},
        texts.ADMIN_ADD_CANCELED: {},
        texts.ADMIN_OWNER_PROTECTED: {"user_link": "@owner"},
        texts.ADMIN_SELF_PROTECTED: {},
        texts.ADMIN_LAST_OWNER_PROTECTED: {},
        texts.ADMIN_DECISION_OK: {"booking_id": 3},
        texts.ADMIN_DECISION_NO: {"booking_id": 3},
        texts.ADMIN_NOT_STARTED_WARNING: {"user_link": "@new"},
        texts.PREPAY_REQUIRED_ALERT: {},
        texts.ADMIN_NOT_ACTIVE: {},
        texts.ADMIN_NO_ACCESS: {},
        texts.ADMINS_EMPTY: {},
    }
    for template, payload in samples.items():
        rendered = texts.render(template, club, **payload)
        check(
            f"Шаблон «{template.splitlines()[0][:38]}…» рендерится", "{" not in rendered
        )

    check(
        "Данные клуба подставляются в шаблоны",
        club.admin_phone in texts.render(texts.CONSENT_TEXT, club),
    )
    check(
        "Смена телефона в .env меняет текст",
        "+7 900 000-00-00"
        in texts.render(texts.CONSENT_TEXT, ClubInfo(admin_phone="+7 900 000-00-00")),
    )
    check(
        "Смена имени администратора меняет текст",
        "Пётр" in texts.render(texts.CONTACTS_LIGHT, ClubInfo(admin_name="Пётр"))
        if hasattr(texts, "CONTACTS_LIGHT")
        else "Пётр"
        in texts.render(texts._CONTACTS_TEMPLATE, ClubInfo(admin_name="Пётр")),
    )

    saturday_check, tuesday_check = date(2026, 10, 17), date(2026, 10, 20)
    check_equal(
        "Пакет угадывается по строке времени: ночь",
        texts.package_key_from_slot("Ночь (22:00–08:00)"),
        texts.TIME_NIGHT_KEY,
    )
    check_equal(
        "Пакет угадывается по строке времени: утро",
        texts.package_key_from_slot("Утро (8:00–13:00)"),
        texts.TIME_MORNING_KEY,
    )
    check_equal(
        "Пакет угадывается по строке времени: день",
        texts.package_key_from_slot("День (13:00–17:00)"),
        texts.TIME_DAY_KEY,
    )
    check(
        "«Сейчас» — не пакет",
        texts.package_key_from_slot("Сейчас (с 17:51, 23.10.2026)") is None,
    )
    check_equal(
        "Сумма заявки считается по названию тарифа и строке времени",
        texts.amount_for_booking("Standart", "Ночь (22:00–08:00)", saturday_check),
        650,
    )
    check_equal(
        "Сумма ночи в будни считается по будничной цене",
        texts.amount_for_booking("Standart", "Ночь (22:00–08:00)", tuesday_check),
        550,
    )
    check(
        "Для почасовой игры сумма заранее неизвестна",
        texts.amount_for_booking(
            "Standart", "Сейчас (с 17:51, 23.10.2026)", saturday_check
        )
        is None,
    )
    check(
        "Неизвестный тариф — без суммы",
        texts.amount_for_booking("Люкс", "Ночь (22:00–08:00)", saturday_check) is None,
    )

    check(
        "Подсказка цены для ночи точная",
        "750 ₽" in texts.tariff_price_hint("vip", "night", date(2026, 10, 20)),
    )
    check(
        "Подсказка цены для ночи в выходные — 850 ₽",
        "850 ₽" in texts.tariff_price_hint("vip", "night", date(2026, 10, 17)),
    )
    check(
        "Подсказка цены для «Сейчас» — почасовая",
        "170 ₽/час" in texts.tariff_price_hint("vip", "now", date(2026, 10, 17)),
    )
    check(
        "Подпись кнопки тарифа содержит цену",
        "₽/час" in texts.tariff_button_label(texts.TARIFFS[0]),
    )
    check(
        "Подпись к чеку укладывается в лимит Telegram (1024)",
        len(
            texts.render(
                texts.NEW_BOOKING_CAPTION,
                club,
                booking_id=1,
                tariff="S VIP",
                time_slot="Ночь (22:00–08:00)",
                client_name="И",
                user_link="@user",
                created_at="23.10.2026 17:51",
                amount="<b>950 ₽</b>",
                payment_note=texts.NEW_BOOKING_RECEIPT_NOTE,
                admins_count=3,
            )
        )
        <= 1024,
    )

    for faq_item in texts.FAQ_ITEMS:
        rendered_faq = texts.faq_answer(faq_item, club)
        check(
            f"FAQ[{faq_item.key}]: ответ отрендерился без плейсхолдеров",
            "{" not in rendered_faq and len(rendered_faq) <= 4096,
        )
    check(
        "В HUD-шаблонах не осталось незакрытых плейсхолдеров",
        "{" not in texts.render(texts.SCREEN_MENU_BODY, club)
        and "{"
        not in texts.render(
            texts.SCREEN_PRICES_BODY,
            club,
            morning_slot=texts.MORNING_SLOT,
            day_slot=texts.DAY_SLOT,
            night_slot=texts.NIGHT_SLOT,
        ),
    )


# --------------------------------------------------------------------------- #
# 6. Сквозной сценарий: клиент → админ
# --------------------------------------------------------------------------- #
async def test_full_scenario(db: Database, cfg: AppConfig, client_id: int) -> None:
    from bot.ui.screens import Screen, ScreenCB, nav

    print("\n— Сквозной сценарий (клиент → админ) —")
    bot = BotSpy()
    state = make_fsm_context(client_id)
    admin_state = make_fsm_context(ADMIN_ID)

    # --- /start без согласия ------------------------------------------------ #
    mark = bot.mark()
    start_message = make_message(user_id=client_id, bot=bot, text="/start")
    await client_handlers.cmd_start(start_message, state, db, cfg)
    sent = bot.new_texts(mark)
    check(
        "Без согласия /start показывает текст 152-ФЗ",
        any("152-ФЗ" in text for text in sent),
    )
    consent_markup = markup_of(bot.new_messages(mark)[0])
    check(
        "Без согласия показывается только кнопка «✅ Я согласен»",
        getattr(consent_markup, "inline_keyboard", None) is not None
        and consent_markup.inline_keyboard[0][0].text == "✅ Я согласен",
    )
    check("Без согласия меню не выдаётся", "ГЛАВНОЕ МЕНЮ" not in "".join(sent))

    # --- кнопка «Я согласен» — то же фото-сообщение становится экраном меню - #
    mark = bot.mark()
    consent_callback = make_callback(
        inline_keyboards.CB_CONSENT_GIVE, user_id=client_id, bot=bot
    )
    await client_handlers.cb_consent_give(consent_callback, state, db, cfg)
    check(
        "После кнопки согласие записано в базу",
        (await db.has_consent(client_id)) is True,
    )
    edits = [p for n, p in bot.since(mark) if n == "EditMessageMedia"]
    check(
        "Сообщение согласия перезаписано без нового сообщения (editMessageMedia, не send)",
        bool(edits)
        and not [p for n, p in bot.since(mark) if n in ("SendMessage", "SendPhoto")],
    )
    menu_text = text_of(edits[0])
    check(
        "На экране открыто главное меню демо-клуба",
        cfg.club.name in menu_text and "ГЛАВНОЕ МЕНЮ" in menu_text,
    )
    check("В подписи нет code-блоков <pre>", "<pre>" not in menu_text)
    menu_btns = [b.text for row in markup_of(edits[0]).inline_keyboard for b in row]
    check(
        "В меню гостя 7 inline-кнопок без админки",
        len(menu_btns) == 7 and "🛠 Админ-панель" not in menu_btns,
    )
    check(
        "Тост о фиксации согласия отправлен",
        any("Согласие зафиксировано" in text_of(p) for p in bot.answers()),
    )

    # --- повторный /start уже с согласием ----------------------------------- #
    mark = bot.mark()
    await client_handlers.cmd_start(
        make_message(user_id=client_id, bot=bot, text="/start"), state, db, cfg
    )
    check(
        "После согласия /start открывает экран меню",
        any("ГЛАВНОЕ МЕНЮ" in t for t in bot.step_texts(mark)),
    )

    # --- шаг 1: вход в мастер по inline-кнопке «🎮 Забронировать место» ----- #
    mark = bot.mark()
    await client_handlers.cb_navigate(
        make_callback(nav(Screen.BOOK_TARIFF), user_id=client_id, bot=bot),
        ScreenCB(screen=Screen.BOOK_TARIFF, arg=""),
        state,
        db,
        cfg,
    )
    check_equal(
        "Шаг 1: состояние выбора тарифа",
        await state.get_state(),
        BookingFSM.choosing_tariff.state,
    )
    tariff_payload = bot.step_payloads(mark)[-1]
    check(
        "Шаг 1 нарисован правкой того же медиа-экрана",
        any(n == "EditMessageMedia" for n, _ in bot.since(mark)),
    )
    check(
        "Шаг 1 показывает залы",
        "Standart" in text_of(tariff_payload) and "S VIP" in text_of(tariff_payload),
    )
    tariff_btns = [b for row in markup_of(tariff_payload).inline_keyboard for b in row]
    check("Шаг 1 содержит 4 зала + Назад + Отменить", len(tariff_btns) == 6)

    mark = bot.mark()
    await client_handlers.cb_choose_tariff(
        make_callback(
            inline_keyboards.tariff_callback("svip"), user_id=client_id, bot=bot
        ),
        state,
        db,
        cfg,
    )
    check_equal(
        "Шаг 2: состояние выбора времени",
        await state.get_state(),
        BookingFSM.choosing_time.state,
    )
    check_equal(
        "Тариф сохранён в FSM", (await state.get_data()).get(DATA_TARIFF_KEY), "svip"
    )
    edited = [p for n, p in bot.since(mark) if n == "EditMessageMedia"]
    check(
        "Сообщение шага 1 заменено шагом 2", edited and "ШАГ 2/4" in text_of(edited[0])
    )
    check(
        "Шаг 2 показывает текущее время клуба", "Сейчас в клубе" in text_of(edited[0])
    )

    # --- «‹ Шаг назад» возвращает на шаг 1, затем снова выбираем зал -------- #
    await client_handlers.cb_booking_back(
        make_callback(inline_keyboards.CB_BOOKING_BACK, user_id=client_id, bot=bot),
        state,
        db,
        cfg,
    )
    check_equal(
        "«Шаг назад» вернул на выбор зала",
        await state.get_state(),
        BookingFSM.choosing_tariff.state,
    )
    await client_handlers.cb_choose_tariff(
        make_callback(
            inline_keyboards.tariff_callback("svip"), user_id=client_id, bot=bot
        ),
        state,
        db,
        cfg,
    )

    # --- шаг 2: «Другое» → время текстом ------------------------------------ #
    await client_handlers.cb_choose_time(
        make_callback(
            inline_keyboards.time_callback("other"), user_id=client_id, bot=bot
        ),
        state,
        db,
        cfg,
    )
    check_equal(
        "Шаг 2б: состояние ввода своего времени",
        await state.get_state(),
        BookingFSM.entering_custom_time.state,
    )

    mark = bot.mark()
    await client_handlers.msg_enter_custom_time(
        make_message(user_id=client_id, bot=bot, text="когда-нибудь потом"),
        state,
        db,
        cfg,
    )
    check(
        "Время без цифр отклоняется подсказкой на том же экране",
        any(texts.INVALID_CUSTOM_TIME in t for t in bot.step_texts(mark)),
    )
    check(
        "Сообщение клиента с ошибкой удалено, чтобы не засорять чат",
        any(n == "DeleteMessage" for n, _ in bot.since(mark)),
    )
    check_equal(
        "После ошибки состояние не сбрасывается",
        await state.get_state(),
        BookingFSM.entering_custom_time.state,
    )

    await client_handlers.msg_enter_custom_time(
        make_message(user_id=client_id, bot=bot, text="сегодня в 18:30 на 3 часа"),
        state,
        db,
        cfg,
    )
    check_equal(
        "Шаг 3: состояние ввода имени",
        await state.get_state(),
        BookingFSM.entering_name.state,
    )
    check_equal(
        "Своё время сохранено",
        (await state.get_data()).get(DATA_TIME_SLOT),
        "сегодня в 18:30 на 3 часа",
    )

    mark = bot.mark()
    await client_handlers.msg_enter_name(
        make_message(user_id=client_id, bot=bot, text="http://spam"), state, db, cfg
    )
    check(
        "Некорректное имя отклоняется",
        any(texts.INVALID_NAME in t for t in bot.step_texts(mark)),
    )
    check_equal(
        "Сценарий не прерывается",
        await state.get_state(),
        BookingFSM.entering_name.state,
    )

    # --- шаг 3: имя → шаг 4 (способ оплаты) --------------------------------- #
    mark = bot.mark()
    await client_handlers.msg_enter_name(
        make_message(user_id=client_id, bot=bot, text="алексей"), state, db, cfg
    )
    check_equal(
        "Шаг 4: выбор способа оплаты",
        await state.get_state(),
        BookingFSM.choosing_payment.state,
    )
    payment_payload = bot.step_payloads(mark)[-1]
    payment_text = text_of(payment_payload)
    check(
        "На шаге оплаты показаны демо-реквизиты + номер",
        cfg.club.payment_system in payment_text
        and cfg.club.payment_card in payment_text,
    )
    check(
        "На шаге оплаты указано имя получателя", cfg.club.payment_holder in payment_text
    )
    check("Имя нормализовано до «Алексей»", "Алексей" in payment_text)
    check("Тариф подставлен в шаг оплаты", "S VIP" in payment_text)
    check(
        "Для будней и своего времени предоплата не требуется",
        "на месте" in payment_text,
    )
    pay_buttons = [b for row in markup_of(payment_payload).inline_keyboard for b in row]
    check(
        "Доступны оба способа + копирование номера + Шаг назад + Отменить",
        len(pay_buttons) == 5
        and any("чек" in b.text for b in pay_buttons)
        and any("на месте" in b.text for b in pay_buttons)
        and any(getattr(b, "copy_text", None) is not None for b in pay_buttons)
        and any("Шаг назад" in b.text for b in pay_buttons),
    )

    # --- шаг 4: клиент выбирает «оплатил — отправить чек» -------------------- #
    mark = bot.mark()
    await client_handlers.cb_choose_payment(
        make_callback(inline_keyboards.CB_PAY_CHECK, user_id=client_id, bot=bot),
        state,
        db,
        cfg,
    )
    check_equal(
        "Шаг 4б: ожидание фото чека",
        await state.get_state(),
        BookingFSM.waiting_receipt.state,
    )
    waiting_text = " ".join(bot.step_texts(mark))
    check("После выбора чека бот ждёт ФОТО чека", "ФОТО" in waiting_text)
    check("В ожидании чека повторены реквизиты", cfg.club.payment_card in waiting_text)

    # --- текст вместо фото --------------------------------------------------- #
    mark = bot.mark()
    await client_handlers.msg_receipt_expected(
        make_message(user_id=client_id, bot=bot, text="я оплатил"), state, db, cfg
    )
    check(
        "Текст вместо фото — просьба прислать именно фото на том же экране",
        any("именно" in t and "ФОТО" in t for t in bot.step_texts(mark)),
    )

    # --- шаг 4: фото чека ---------------------------------------------------- #
    mark = bot.mark()
    await client_handlers.msg_receive_receipt(
        make_message(user_id=client_id, bot=bot, with_photo=True), state, db, cfg
    )
    check("FSM очищен после создания заявки", (await state.get_state()) is None)
    done_payload = [
        p for p in bot.step_payloads(mark) if getattr(p, "chat_id", None) == client_id
    ][-1]
    client_reply = text_of(done_payload)
    check(
        "Клиенту показан экран «ЗАЯВКА ОТПРАВЛЕНА»", "ЗАЯВКА ОТПРАВЛЕНА" in client_reply
    )
    check("На экране подтверждён приём чека", "чек отправлен" in client_reply)
    done_btns = [b.text for row in markup_of(done_payload).inline_keyboard for b in row]
    check(
        "Под экраном есть «📋 Мои брони» и «🏠 В меню»",
        "📋 Мои брони" in done_btns and "🏠 В меню" in done_btns,
    )

    admin_photos = [p for n, p in bot.since(mark) if n == "SendPhoto"]
    check("Админу отправлено одно фото чека", len(admin_photos) == 1)
    admin_photo = admin_photos[0]
    check_equal(
        "Уведомление ушло администратору из .env", admin_photo.chat_id, ADMIN_ID
    )
    check_equal(
        "В базу сохранён file_id самого большого фото",
        admin_photo.photo,
        "AgAC_size_1280",
    )
    caption = text_of(admin_photo)
    check("В подписи чека есть «НОВАЯ БРОНЬ»", "НОВАЯ БРОНЬ" in caption)
    check(
        "В подписи есть тариф, время, имя",
        all(x in caption for x in ("S VIP", "18:30", "Алексей")),
    )
    check("В подписи есть ссылка на клиента", "Юзер:" in caption)
    decision_markup = markup_of(admin_photo)
    check_equal(
        "Первая кнопка под чеком — «✅ Подтвердить»",
        decision_markup.inline_keyboard[0][0].text,
        "✅ Подтвердить",
    )
    check_equal(
        "Вторая кнопка под чеком — «❌ Отклонить»",
        decision_markup.inline_keyboard[0][1].text,
        "❌ Отклонить",
    )
    check(
        "В кнопки зашит id брони",
        inline_keyboards.parse_admin_decision(
            decision_markup.inline_keyboard[0][0].callback_data
        )[1]
        > 0,
    )

    created = await db.list_bookings(
        BookingStatus.PENDING, limit=200, oldest_first=True
    )
    target = next(b for b in created if b.client_name == "Алексей")
    check_equal(
        "Заявка сохранена со статусом pending", target.status, BookingStatus.PENDING
    )
    check_equal("В заявке сохранён тариф", target.tariff, "S VIP")

    # --- админ: список активных броней --------------------------------------- #
    mark = bot.mark()
    await admin_handlers.cb_pending_list(
        make_callback(
            inline_keyboards.admin_list_callback(0), user_id=ADMIN_ID, bot=bot
        ),
        admin_state,
        db,
        cfg,
    )
    edits = [
        p
        for n, p in bot.since(mark)
        if n in ("EditMessageMedia", "SendPhoto", "SendMessage")
    ]
    list_text = text_of(edits[-1])
    check("Список активных броней отрисован", "АКТИВНЫЕ БРОНИ" in list_text)
    check("В списке видны номера заявок", "№" in list_text)
    check("На странице не больше 5 заявок", list_text.count("👤") <= 5)
    check(
        "У каждой заявки в списке есть кнопки решения и Назад/В меню",
        len(markup_of(edits[-1]).inline_keyboard) >= 3,
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
        admin_state,
        db,
        bot,
        cfg,
    )
    check_equal(
        "Статус заявки — approved",
        (await db.get_booking(target.id)).status,
        BookingStatus.APPROVED,
    )

    client_notifications = [p for n, p in bot.since(mark) if n == "SendMessage"]
    check("Клиенту отправлено одно уведомление", len(client_notifications) == 1)
    client_text = text_of(client_notifications[0])
    check_equal("Уведомление ушло клиенту", client_notifications[0].chat_id, client_id)
    check("Клиенту: «Ваша бронь подтверждена!»", "бронь подтверждена" in client_text)
    check(
        "Клиенту показаны место и время",
        "S VIP" in client_text and "18:30" in client_text,
    )

    caption_edits = [p for n, p in bot.since(mark) if n == "EditMessageCaption"]
    check("Подпись чека у админа обновлена", bool(caption_edits))
    check(
        "В подписи появилась отметка «ПОДТВЕРЖДЕНО»",
        "ПОДТВЕРЖДЕНО" in text_of(caption_edits[0]),
    )
    check(
        "Кнопки решения убраны после подтверждения", markup_of(caption_edits[0]) is None
    )
    check(
        "Админ получил тост о подтверждении",
        any(
            "подтверждена" in text_of(p)
            for p in [q for n, q in bot.since(mark) if n == "AnswerCallbackQuery"]
        ),
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
        admin_state,
        db,
        bot,
        cfg,
    )
    check(
        "Повторное подтверждение не дублирует уведомление клиенту",
        not [p for n, p in bot.since(mark) if n == "SendMessage"],
    )
    alerts = [p for n, p in bot.since(mark) if n == "AnswerCallbackQuery"]
    check(
        "Админ получил alert о повторном решении",
        alerts and alerts[0].show_alert is True,
    )
    check(
        "В alert указан текущий статус", alerts and "подтверждена" in text_of(alerts[0])
    )

    # --- админ: отклонение другой заявки (из списка, без фото) --------------- #
    other_booking = await db.add_booking(
        client_id, "Double", "Сейчас (с 22:00, 23.10.2026)", "Олег", "AgAC_second"
    )
    mark = bot.mark()
    await admin_handlers.cb_decision(
        make_callback(
            inline_keyboards.admin_reject_callback(other_booking.id),
            user_id=ADMIN_ID,
            bot=bot,
        ),
        admin_state,
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
    check(
        "При отклонении клиенту дан телефон клуба",
        cfg.club.admin_phone in rejected_text,
    )
    check(
        "При отклонении сказано, что бронь не подтверждена",
        "не смогли подтвердить" in rejected_text,
    )
    check(
        "Сообщение админа со списком перерисовано",
        any(n in ("EditMessageMedia", "SendPhoto") for n, _ in bot.since(mark)),
    )

    # --- админ: панель сводки (и кнопкой меню, и /admin) --------------------- #
    mark = bot.mark()
    await admin_handlers.cmd_admin(
        make_message(
            user_id=ADMIN_ID,
            bot=bot,
            text="/admin",
            username="admin",
            first_name="Админ",
        ),
        admin_state,
        db,
        cfg,
    )
    stats_payload = bot.step_payloads(mark)[-1]
    stats_text = text_of(stats_payload)
    check("Статистика содержит «Заявок всего»", "Заявок всего" in stats_text)
    check("Статистика содержит «Ожидают»", "Ожидают" in stats_text)
    check("Статистика содержит «Одобрено»", "Одобрено" in stats_text)
    check("Статистика содержит «Отклонено»", "Отклонено" in stats_text)
    check("Статистика показывает число клиентов", "Клиентов" in stats_text)
    check(
        "Панель содержит кнопку «📋 Активные брони» и «◀ Назад»",
        any(
            b.text.startswith("📋 Активные брони")
            for row in markup_of(stats_payload).inline_keyboard
            for b in row
        )
        and any(
            b.text == "◀ Назад"
            for row in markup_of(stats_payload).inline_keyboard
            for b in row
        ),
    )

    mark = bot.mark()
    await admin_handlers.cb_refresh_stats(
        make_callback(inline_keyboards.CB_ADMIN_STATS, user_id=ADMIN_ID, bot=bot),
        admin_state,
        db,
        cfg,
    )
    check(
        "Кнопка обновления статистики работает",
        any(n == "EditMessageMedia" for n, _ in bot.since(mark)),
    )

    await state.storage.close()
    await admin_state.storage.close()


# --------------------------------------------------------------------------- #
# 7. Отмена, навигация «Назад» и протухшие кнопки
# --------------------------------------------------------------------------- #
async def test_cancel_and_outdated(db: Database, cfg: AppConfig) -> None:
    from bot.ui.screens import SCREEN_BACK, Screen, ScreenCB, nav

    print("\n— Отмена, стек «Назад» и разделы —")
    bot = BotSpy()
    state = make_fsm_context(CANCEL_CLIENT_ID)
    await state.set_state(BookingFSM.entering_name)

    mark = bot.mark()
    await client_handlers.cmd_cancel(
        make_message(user_id=CANCEL_CLIENT_ID, bot=bot, text="/cancel"), state, db, cfg
    )
    check("После /cancel состояние сброшено", (await state.get_state()) is None)
    cancel_payload = bot.step_payloads(mark)[-1]
    check(
        "Клиенту сообщено об отмене и показано меню",
        texts.BOOKING_CANCELED in text_of(cancel_payload),
    )
    check(
        "После отмены показаны inline-кнопки главного меню",
        markup_of(cancel_payload).inline_keyboard[0][0].text
        == "🎮 Забронировать место",
    )

    mark = bot.mark()
    await client_handlers.cmd_cancel(
        make_message(user_id=CANCEL_CLIENT_ID, bot=bot, text="/cancel"), state, db, cfg
    )
    check(
        "/cancel без активного бронирования отвечает на том же экране",
        any(texts.BOOKING_NOT_STARTED in t for t in bot.step_texts(mark))
        and any(n == "EditMessageMedia" for n, _ in bot.since(mark)),
    )

    await state.set_state(BookingFSM.choosing_time)
    mark = bot.mark()
    await client_handlers.cb_cancel_booking(
        make_callback(
            inline_keyboards.CB_BOOKING_CANCEL, user_id=CANCEL_CLIENT_ID, bot=bot
        ),
        state,
        db,
        cfg,
    )
    check(
        "Кнопка «Отменить» перерисовывает экран в меню",
        any(n == "EditMessageMedia" for n, _ in bot.since(mark)),
    )
    check("После кнопки отмены состояние сброшено", (await state.get_state()) is None)

    mark = bot.mark()
    outdated = make_callback(
        inline_keyboards.tariff_callback("vip"), user_id=CANCEL_CLIENT_ID, bot=bot
    )
    await client_handlers.cb_booking_outdated(outdated)
    alerts = [p for n, p in bot.since(mark) if n == "AnswerCallbackQuery"]
    check(
        "Протухшая кнопка бронирования даёт alert",
        alerts and alerts[0].show_alert is True,
    )

    mark = bot.mark()
    await client_handlers.msg_photo_without_booking(
        make_message(user_id=CANCEL_CLIENT_ID, bot=bot, with_photo=True), state, db, cfg
    )
    check(
        "Фото вне бронирования объясняет, что делать",
        any(texts.PHOTO_WITHOUT_BOOKING in t for t in bot.step_texts(mark)),
    )

    # Фото, присланное посреди сценария, не должно обрывать бронирование
    await state.set_state(BookingFSM.entering_name)
    mark = bot.mark()
    await client_handlers.msg_photo_without_booking(
        make_message(user_id=CANCEL_CLIENT_ID, bot=bot, with_photo=True), state, db, cfg
    )
    check(
        "Фото на шаге ввода имени даёт подсказку про имя",
        any("указать имя" in t for t in bot.step_texts(mark)),
    )
    check_equal(
        "Сценарий при этом продолжается",
        await state.get_state(),
        BookingFSM.entering_name.state,
    )
    await state.set_state(None)

    mark = bot.mark()
    await client_handlers.msg_unknown(
        make_message(user_id=CANCEL_CLIENT_ID, bot=bot, text="что-то странное"),
        state,
        db,
        cfg,
    )
    check(
        "Непонятное сообщение получает подсказку над меню",
        any(texts.UNKNOWN_MESSAGE in t for t in bot.step_texts(mark)),
    )

    # --- переходы между экранами правят ОДНО сообщение, «Назад» помнит путь - #
    async def go(screen: str, arg: str = "") -> str:
        m = bot.mark()
        await client_handlers.cb_navigate(
            make_callback(nav(screen, arg), user_id=CANCEL_CLIENT_ID, bot=bot),
            ScreenCB(screen=screen, arg=arg),
            state,
            db,
            cfg,
        )
        assert not [p for n, p in bot.since(m) if n in ("SendMessage", "SendPhoto")], (
            "лишнее новое сообщение при навигации"
        )
        return bot.step_texts(m)[-1]

    price_text = await go(Screen.PRICES)
    check(
        "Экран «Залы и цены» показывает оба прейскуранта",
        "ЗАЛЫ И ЦЕНЫ" in price_text and "Standart" in price_text,
    )

    faq_list = await go(Screen.FAQ)
    check(
        "Экран FAQ — список вопросов, а не простыня",
        "ЧАСТЫЕ ВОПРОСЫ" in faq_list and "Выберите вопрос" in faq_list,
    )

    faq_night = await go(Screen.FAQ_ITEM, "night")
    check(
        "Кнопка вопроса открывает ответ в том же сообщении", "22:00–08:00" in faq_night
    )

    faq_scam = await go(Screen.FAQ_ITEM, "scam")
    check(
        "В ответе про мошенников назван единый номер", cfg.club.payment_card in faq_scam
    )

    # Возврат по стеку: faq_item(scam) → faq_item(night) → faq → prices → menu
    back1 = await go(SCREEN_BACK)
    check("«Назад» вернул на предыдущий вопрос FAQ", "22:00–08:00" in back1)
    back2 = await go(SCREEN_BACK)
    check("Ещё раз «Назад» — к списку вопросов", "Выберите вопрос" in back2)
    back3 = await go(SCREEN_BACK)
    check("Ещё раз «Назад» — к ценам, откуда пришли", "ЗАЛЫ И ЦЕНЫ" in back3)

    rules_text = await go(Screen.RULES)
    check("Правила открываются экраном", "ПРАВИЛА КЛУБА" in rules_text)

    contacts_text = await go(Screen.CONTACTS)
    check(
        "В контактах кликабельный телефон и адрес",
        cfg.club.admin_phone in contacts_text and cfg.club.address in contacts_text,
    )

    bookings_text = await go(Screen.BOOKINGS)
    check("Раздел «Мои брони» отрисован", "МОИ БРОНИ" in bookings_text)

    # Старый текст reply-кнопки (у клиента могла остаться старая клавиатура)
    mark = bot.mark()
    await client_handlers.msg_unknown(
        make_message(
            user_id=CANCEL_CLIENT_ID, bot=bot, text=reply_keyboards.MENU_PRICE
        ),
        state,
        db,
        cfg,
    )
    check(
        "Старая reply-кнопка по-прежнему открывает нужный экран",
        any("ЗАЛЫ И ЦЕНЫ" in t for t in bot.step_texts(mark)),
    )

    mark = bot.mark()
    await client_handlers.cmd_privacy(
        make_message(user_id=CANCEL_CLIENT_ID, bot=bot, text="/privacy"), state, db, cfg
    )
    privacy_payload = bot.step_payloads(mark)[-1]
    privacy_text = text_of(privacy_payload)
    check("Команда /privacy показывает политику ПДн", "152-ФЗ" in privacy_text)
    privacy_btns = [
        b for row in markup_of(privacy_payload).inline_keyboard for b in row
    ]
    check(
        "В /privacy есть кнопка отзыва согласия и «◀ Назад»",
        any(b.callback_data == inline_keyboards.CB_CONSENT_REVOKE for b in privacy_btns)
        and any(b.text == "◀ Назад" for b in privacy_btns),
    )

    # --- отзыв согласия: бот перестаёт работать ----------------------------- #
    mark = bot.mark()
    await client_handlers.cb_consent_revoke(
        make_callback(
            inline_keyboards.CB_CONSENT_REVOKE, user_id=CANCEL_CLIENT_ID, bot=bot
        )
    )
    check(
        "Отзыв согласия требует подтверждения",
        "Отзыв согласия" in " ".join(bot.step_texts(mark)),
    )
    check(
        "Подтверждение отзыва снабжено двумя кнопками",
        len(markup_of(bot.step_payloads(mark)[0]).inline_keyboard[0]) == 2,
    )

    mark = bot.mark()
    await client_handlers.cb_consent_revoke_yes(
        make_callback(
            inline_keyboards.CB_CONSENT_REVOKE_YES, user_id=CANCEL_CLIENT_ID, bot=bot
        ),
        state,
        db,
        cfg,
    )
    revoked_text = " ".join(bot.step_texts(mark))
    check("Согласие снято в базе", (await db.has_consent(CANCEL_CLIENT_ID)) is False)
    check("Клиенту сообщено об отзыве", "отозвано" in revoked_text.lower())
    check(
        "После отзыва предложено вернуть согласие",
        any(
            b.callback_data == inline_keyboards.CB_CONSENT_GIVE
            for row in markup_of(bot.step_payloads(mark)[0]).inline_keyboard
            for b in row
        ),
    )

    # Согласие возвращается той же кнопкой.
    mark = bot.mark()
    await client_handlers.cb_consent_give(
        make_callback(
            inline_keyboards.CB_CONSENT_GIVE, user_id=CANCEL_CLIENT_ID, bot=bot
        ),
        state,
        db,
        cfg,
    )
    check(
        "Согласие можно вернуть той же кнопкой",
        (await db.has_consent(CANCEL_CLIENT_ID)) is True,
    )

    mark = bot.mark()
    await client_handlers.cmd_help(
        make_message(user_id=CANCEL_CLIENT_ID, bot=bot, text="/help"), state, db, cfg
    )
    check(
        "Команда /help показывает справку",
        any("Как пользоваться ботом" in t for t in bot.step_texts(mark)),
    )

    await state.storage.close()


# --------------------------------------------------------------------------- #
# 8. Деградация: Telegram не дал отредактировать сообщение
# --------------------------------------------------------------------------- #
class NoEditBot(BotSpy):
    """«Бот», у которого правка сообщений и медиа всегда заканчивается ошибкой Telegram."""

    async def edit_message_text(self, **kwargs: Any) -> None:
        raise TelegramBadRequest(
            EditMessageText(chat_id=1, message_id=1, text="x"),
            "Bad Request: message can't be edited",
        )

    async def edit_message_media(self, **kwargs: Any) -> Any:
        raise TelegramBadRequest(
            EditMessageText(chat_id=1, message_id=1, text="x"),
            "Bad Request: message to edit not found",
        )


async def test_edit_fallback(db: Database, cfg: AppConfig) -> None:
    from bot.ui.screens import Screen, ScreenCB, nav

    print("\n— Отказоустойчивость правки сообщений —")
    bot = NoEditBot()
    state = make_fsm_context(CANCEL_CLIENT_ID + 1)
    admin_state = make_fsm_context(ADMIN_ID)

    await client_handlers.cb_navigate(
        make_callback(nav(Screen.BOOK_TARIFF), user_id=CANCEL_CLIENT_ID + 1, bot=bot),
        ScreenCB(screen=Screen.BOOK_TARIFF, arg=""),
        state,
        db,
        cfg,
    )
    mark = bot.mark()
    await client_handlers.cb_choose_tariff(
        make_callback(
            inline_keyboards.tariff_callback("vip"),
            user_id=CANCEL_CLIENT_ID + 1,
            bot=bot,
        ),
        state,
        db,
        cfg,
    )
    check(
        "Если правка не удалась, шаг 2 отправляется новым сообщением",
        any("ШАГ 2/4" in text for text in bot.new_texts(mark)),
    )
    check_equal(
        "Сценарий при этом не сбивается",
        await state.get_state(),
        BookingFSM.choosing_time.state,
    )

    mark = bot.mark()
    await admin_handlers.cb_pending_list(
        make_callback(
            inline_keyboards.admin_list_callback(0), user_id=ADMIN_ID, bot=bot
        ),
        admin_state,
        db,
        cfg,
    )
    check(
        "Список заявок админа тоже уходит новым сообщением при отказе правки",
        any("АКТИВНЫЕ БРОНИ" in text for text in bot.new_texts(mark)),
    )
    await state.clear()
    await state.storage.close()
    await admin_state.storage.close()


# --------------------------------------------------------------------------- #
# 9. Роутеры, фильтры, middleware
# --------------------------------------------------------------------------- #
def build_dispatcher(
    db: Database, cfg: AppConfig, admins: AdminRegistry | None = None
) -> Dispatcher:
    """Собрать диспетчер ровно так, как это делает :mod:`main`."""
    dispatcher = Dispatcher(storage=MemoryStorage(), db=db, cfg=cfg, admins=admins)
    dispatcher.include_router(admin_handlers.router)  # админ-панель — первой
    dispatcher.include_router(client_handlers.router)  # клиент — вторым
    return dispatcher


def root_filters(observer: Any) -> list[Any]:
    """Роутовые фильтры наблюдателя aiogram (внутреннее поле — только для проверки)."""
    handler = getattr(observer, "_handler", None)
    return [
        getattr(item, "callback", item)
        for item in (getattr(handler, "filters", None) or [])
    ]


async def test_wiring(db: Database, cfg: AppConfig, dispatcher: Dispatcher) -> None:
    print("\n— Сборка роутеров и фильтров —")
    check(
        "Админ-роутер подключён первым (приоритет панели)",
        dispatcher.sub_routers[0].name == "admin",
    )
    check(
        "Клиентский роутер подключён вторым", dispatcher.sub_routers[1].name == "client"
    )

    client_message_handlers = len(client_handlers.router.message.handlers)
    client_callback_handlers = len(client_handlers.router.callback_query.handlers)
    check(
        "Клиентские хендлеры сообщений зарегистрированы",
        client_message_handlers >= 8,
        str(client_message_handlers),
    )
    check(
        "Клиентские хендлеры кнопок зарегистрированы",
        client_callback_handlers >= 6,
        str(client_callback_handlers),
    )
    check(
        "Админские хендлеры сообщений зарегистрированы",
        len(admin_handlers.router.message.handlers) >= 1,
    )
    check(
        "Админские хендлеры кнопок зарегистрированы",
        len(admin_handlers.router.callback_query.handlers) >= 3,
    )

    check(
        "На клиентском роутере стоит middleware согласия (152-ФЗ)",
        any(
            isinstance(m, ConsentMiddleware)
            for m in client_handlers.router.message.outer_middleware
        ),
    )
    check(
        "Middleware согласия стоит и на кнопках",
        any(
            isinstance(m, ConsentMiddleware)
            for m in client_handlers.router.callback_query.outer_middleware
        ),
    )
    check(
        "Админ-роутер защищён фильтром IsAdmin",
        any(isinstance(f, IsAdmin) for f in root_filters(admin_handlers.router.message))
        and any(
            isinstance(f, IsAdmin)
            for f in root_filters(admin_handlers.router.callback_query)
        ),
    )
    check(
        "Роутеры работают только в личных чатах",
        any(
            isinstance(f, IsPrivateChat)
            for f in root_filters(client_handlers.router.message)
        ),
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
    check(
        "IsAdmin пропускает администратора",
        (await is_admin(admin_sender, cfg=cfg)) is True,
    )
    check(
        "IsAdmin не пропускает клиента",
        (await is_admin(client_sender, cfg=cfg)) is False,
    )
    check(
        "IsAdmin без конфигурации запрещает доступ",
        (await is_admin(admin_sender, cfg=None)) is False,
    )

    is_private = IsPrivateChat()
    private_chat = SimpleNamespace(type="private")
    group_chat = SimpleNamespace(type="group")
    check(
        "IsPrivateChat пропускает личный чат",
        (await is_private(object(), event_chat=private_chat)) is True,
    )
    check(
        "IsPrivateChat отсекает группу",
        (await is_private(object(), event_chat=group_chat)) is False,
    )
    check(
        "IsPrivateChat без чата отсекает событие",
        (await is_private(object(), event_chat=None)) is False,
    )

    # --- middleware согласия: блокирует меню и пропускает /start ------------- #
    gate = ConsentMiddleware()
    newbie_id = 424_242_424
    calls: list[str] = []

    async def handler(event: Any, data: dict[str, Any]) -> str:
        calls.append("handler")
        return "handled"

    context: dict[str, Any] = {
        "db": db,
        "event_from_user": TelegramUser(
            id=newbie_id, is_bot=False, first_name="Новичок"
        ),
        "event_chat": Chat(id=newbie_id, type="private"),
    }
    bot = BotSpy()

    menu_event = make_message(
        user_id=newbie_id, bot=bot, text=reply_keyboards.MENU_PRICE
    ).as_(bot)
    result = await gate(handler, menu_event, dict(context))
    check(
        "Без согласия меню не работает (хендлер не вызван)",
        result is None and not calls,
    )
    check(
        "Без согласия показывается напоминание про 152-ФЗ",
        any("152-ФЗ" in t for t in bot.texts()),
    )

    mark = bot.mark()
    start_event = make_message(user_id=newbie_id, bot=bot, text="/start").as_(bot)
    await gate(handler, start_event, dict(context))
    check("Команда /start проходит через middleware согласия", calls == ["handler"])
    check(
        "При пропуске /start middleware ничего не отправляет", bot.new_texts(mark) == []
    )

    await db.upsert_user(newbie_id, "newbie", "Новичок")
    await db.set_consent(newbie_id, True)
    mark = bot.mark()
    menu_after_consent = make_message(
        user_id=newbie_id, bot=bot, text=reply_keyboards.MENU_PRICE
    )
    await gate(handler, menu_after_consent, dict(context))
    check("После согласия middleware пропускает события", len(calls) == 2)
    check("После согласия middleware не шлёт напоминаний", bot.new_texts(mark) == [])

    # Кнопки до согласия
    blocked_callback = make_callback(
        inline_keyboards.tariff_callback("vip"), user_id=newbie_id + 1, bot=bot
    )
    blocked_result = await gate(
        handler,
        blocked_callback,
        {
            **context,
            "event_from_user": TelegramUser(
                id=newbie_id + 1, is_bot=False, first_name="Ещё один"
            ),
        },
    )
    check("Кнопки без согласия блокируются", blocked_result is None)
    check(
        "Блокировка кнопки сопровождается alert'ом",
        any(getattr(p, "show_alert", False) for p in bot.answers()),
    )

    consent_callback = make_callback(
        inline_keyboards.CB_CONSENT_GIVE, user_id=newbie_id + 1, bot=bot
    )
    before = len(calls)
    await gate(
        handler,
        consent_callback,
        {
            **context,
            "event_from_user": TelegramUser(
                id=newbie_id + 1, is_bot=False, first_name="Ещё один"
            ),
        },
    )
    check("Кнопка «Я согласен» проходит через middleware", len(calls) == before + 1)

    # --- Bot с HTML-разметкой (без сетевых вызовов) -------------------------- #
    real_bot = Bot(
        token=TEST_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    check(
        "Bot создаётся с parse_mode=HTML", real_bot.default.parse_mode == ParseMode.HTML
    )
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


async def test_dispatcher_routing(
    db: Database, cfg: AppConfig, dispatcher: Dispatcher
) -> None:
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

    from bot.ui.screens import Screen, nav

    # 1. /start от нового пользователя → согласие 152-ФЗ
    _, sent = await feed(make_message(user_id=user_id, bot=bot, text="/start"))
    check(
        "feed_update: /start показывает согласие 152-ФЗ",
        any("152-ФЗ" in t for t in sent),
    )
    check(
        "feed_update: до согласия меню не выдаётся",
        not any("ГЛАВНОЕ МЕНЮ" in t for t in sent),
    )

    # 2. Кнопка без согласия → блокировка middleware
    _, sent = await feed(make_callback(nav(Screen.PRICES), user_id=user_id, bot=bot))
    check(
        "feed_update: прайс без согласия не показан",
        not any("ЗАЛЫ И ЦЕНЫ" in t for t in sent),
    )

    # 3. Согласие через callback
    await feed(
        make_callback(inline_keyboards.CB_CONSENT_GIVE, user_id=user_id, bot=bot)
    )
    check(
        "feed_update: после кнопки согласие в базе",
        (await db.has_consent(user_id)) is True,
    )

    # 4. Inline-навигация работает: цены и FAQ правят то же сообщение
    _, sent = await feed(make_callback(nav(Screen.PRICES), user_id=user_id, bot=bot))
    check(
        "feed_update: с согласием прайс открывается",
        any("ЗАЛЫ И ЦЕНЫ" in t for t in sent),
    )

    _, sent = await feed(make_callback(nav(Screen.FAQ), user_id=user_id, bot=bot))
    check(
        "feed_update: открыт список вопросов FAQ",
        any("ЧАСТЫЕ ВОПРОСЫ" in t for t in sent),
    )
    _, sent = await feed(
        make_callback(nav(Screen.FAQ_ITEM, "faceit"), user_id=user_id, bot=bot)
    )
    check("feed_update: открыт ответ вопроса FAQ", any("FACEIT" in t for t in sent))

    # 4b. Попытка открыть админ-панель без прав через nav:admin блокируется
    mark = bot.mark()
    await feed(make_callback(nav(Screen.ADMIN), user_id=user_id, bot=bot))
    check(
        "feed_update: nav:admin без прав блокируется alert'ом",
        any(
            getattr(p, "show_alert", False)
            for n, p in bot.since(mark)
            if n == "AnswerCallbackQuery"
        ),
    )

    # 5. Полный сценарий бронирования через апдейты (FSM общий между ними)
    _, sent = await feed(
        make_callback(nav(Screen.BOOK_TARIFF), user_id=user_id, bot=bot)
    )
    check(
        "feed_update: старт бронирования показывает залы",
        any("Standart" in t for t in sent),
    )

    _, sent = await feed(
        make_callback(
            inline_keyboards.tariff_callback("double"), user_id=user_id, bot=bot
        )
    )
    check("feed_update: шаг 2 — выбор времени", any("ШАГ 2/4" in t for t in sent))

    _, sent = await feed(
        make_callback(inline_keyboards.time_callback("night"), user_id=user_id, bot=bot)
    )
    check("feed_update: шаг 3 — ввод имени", any("ШАГ 3/4" in t for t in sent))
    check(
        "feed_update: ночной тариф подставлен в шаг",
        any("22:00–08:00" in t for t in sent),
    )

    today = datetime.now(cfg.timezone).date()
    _, sent = await feed(make_message(user_id=user_id, bot=bot, text="алексей"))
    check(
        "feed_update: шаг 4 — выбор способа оплаты", any("ШАГ 4/4" in t for t in sent)
    )
    check("feed_update: имя нормализовано", any("Алексей" in t for t in sent))
    check(
        "feed_update: на шаге оплаты названа сумма", any("К оплате" in t for t in sent)
    )
    if texts.prepayment_required("night", today):
        check(
            "feed_update: ночь в выходные — только по предоплате",
            any("предоплате" in t for t in sent),
        )
    else:
        check(
            "feed_update: ночь в будни — можно платить на месте",
            any("на месте" in t for t in sent),
        )

    _, sent = await feed(
        make_callback(inline_keyboards.CB_PAY_CHECK, user_id=user_id, bot=bot)
    )
    check(
        "feed_update: после выбора чека бот ждёт фото", any("ФОТО" in t for t in sent)
    )

    _, sent = await feed(
        make_message(user_id=user_id, bot=bot, text="а где реквизиты?")
    )
    check(
        "feed_update: текст вместо чека — просьба прислать фото",
        any("именно" in t for t in sent),
    )

    _, sent = await feed(make_message(user_id=user_id, bot=bot, with_photo=True))
    check("feed_update: чек принят", any("ЗАЯВКА ОТПРАВЛЕНА" in t for t in sent))
    admin_photos = [
        p
        for n, p in bot.calls
        if n == "SendPhoto" and getattr(p, "chat_id", None) == ADMIN_ID
    ]
    check("feed_update: чек ушёл администратору", bool(admin_photos))

    pending = await db.list_bookings(BookingStatus.PENDING, limit=50, oldest_first=True)
    live = next(b for b in pending if b.client_name == "Алексей")
    check_equal(
        "feed_update: заявка в базе со статусом pending",
        live.status,
        BookingStatus.PENDING,
    )

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
    check_equal(
        "feed_update: статус стал approved",
        (await db.get_booking(live.id)).status,
        BookingStatus.APPROVED,
    )
    client_notify = [
        p
        for n, p in bot.since(mark)
        if n == "SendMessage" and getattr(p, "chat_id", None) == user_id
    ]
    check(
        "feed_update: клиенту ушло подтверждение",
        any("бронь подтверждена" in text_of(p) for p in client_notify),
    )

    # 7. Админ-панель: кнопкой из меню у админа и отказ клиенту
    await db.upsert_user(ADMIN_ID, "admin", "Админ")
    await db.set_consent(ADMIN_ID, granted=True)
    _, sent = await feed(
        make_message(
            user_id=ADMIN_ID,
            bot=bot,
            text="/start",
            username="admin",
            first_name="Админ",
        )
    )
    check(
        "feed_update: /start у админа открывает меню",
        any("ГЛАВНОЕ МЕНЮ" in t for t in sent),
    )
    _, sent = await feed(make_callback(nav(Screen.ADMIN), user_id=ADMIN_ID, bot=bot))
    check(
        "feed_update: кнопка «Админ-панель» открывает сводку",
        any("Заявок всего" in t for t in sent),
    )

    _, sent = await feed(make_message(user_id=user_id, bot=bot, text="/admin"))
    check(
        "feed_update: /admin от клиента не показывает статистику",
        not any("Заявок всего" in t for t in sent),
    )
    check(
        "feed_update: /admin от клиента объясняет отсутствие прав",
        any("только для администраторов" in t for t in sent),
    )

    # 7b. Администратор, добавленный через панель, получает доступ без перезапуска
    registry: AdminRegistry = dispatcher.workflow_data["admins"]
    await db.add_admin(ADMIN_ID_2, role=AdminRole.OPERATOR, added_by=ADMIN_ID)
    registry.remember(ADMIN_ID_2)

    _, sent = await feed(make_message(user_id=user_id, bot=bot, text="/admins"))
    check(
        "feed_update: /admins от клиента не показывает список",
        not any("АДМИНИСТРАТОРЫ" in t for t in sent),
    )

    mark = bot.mark()
    _, sent = await feed(
        make_message(
            user_id=ADMIN_ID_2,
            bot=bot,
            text="/admins",
            username="second_admin",
            first_name="Второй",
        )
    )
    check(
        "feed_update: добавленный админ видит список администраторов",
        any("АДМИНИСТРАТОРЫ" in t for t in sent),
    )
    check(
        "feed_update: оператору объяснено, что управление — у владельца",
        any("только владелец" in t for t in sent),
    )
    operator_payload = bot.step_payloads(mark)[-1]
    operator_btns = [
        b for row in markup_of(operator_payload).inline_keyboard for b in row
    ]
    check(
        "feed_update: у оператора нет кнопки «➕ Добавить»",
        not any(
            b.callback_data == inline_keyboards.CB_ADMIN_ADD for b in operator_btns
        ),
    )

    mark = bot.mark()
    await feed(
        make_message(
            user_id=ADMIN_ID,
            bot=bot,
            text="/admins",
            username="admin",
            first_name="Админ",
        )
    )
    owner_payload = bot.step_payloads(mark)[-1]
    check(
        "feed_update: у владельца есть кнопка «➕ Добавить»",
        any(
            b.text == "➕ Добавить"
            for row in markup_of(owner_payload).inline_keyboard
            for b in row
        ),
    )

    # Добавление администратора через апдейты: кнопка → ID → запись в базу
    mark = bot.mark()
    await feed(make_callback(inline_keyboards.CB_ADMIN_ADD, user_id=ADMIN_ID, bot=bot))
    check(
        "feed_update: владелец получил запрос ID нового администратора",
        any("числовой Telegram ID" in t for t in bot.step_texts(mark)),
    )
    _, sent = await feed(
        make_message(
            user_id=ADMIN_ID,
            bot=bot,
            text=str(ADMIN_ID_3),
            username="admin",
            first_name="Админ",
        )
    )
    check(
        "feed_update: новый администратор добавлен",
        await db.get_admin(ADMIN_ID_3) is not None,
    )
    check(
        "feed_update: после добавления показан обновлённый список",
        any("АДМИНИСТРАТОРЫ" in t for t in sent),
    )

    _, sent = await feed(
        make_message(
            user_id=ADMIN_ID_3,
            bot=bot,
            text="/admin",
            username="new_admin",
            first_name="Новый",
        )
    )
    check(
        "feed_update: добавленный через апдейты админ видит статистику",
        any("Заявок всего" in t for t in sent),
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
    group_result = await dispatcher.feed_update(
        bot, make_update(counter, group_message)
    )
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
    check_equal(
        "ADMIN_IDS из .env синхронизированы с базой", synced, (ADMIN_ID, ADMIN_ID_2)
    )
    owner = await db.get_admin(ADMIN_ID)
    second = await db.get_admin(ADMIN_ID_2)
    check("Первый ID из .env стал владельцем", owner is not None and owner.is_owner)
    check(
        "Второй ID из .env стал администратором",
        second is not None and second.role == AdminRole.OPERATOR,
    )
    check("Оба администратора активны", owner.is_active and second.is_active)
    check_equal("Роль владельца называется по-русски", owner.role_title, "владелец")
    check_equal(
        "Повторный startup идемпотентен", await admins.startup(), (ADMIN_ID, ADMIN_ID_2)
    )
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
    check(
        "Фильтр работает и без реестра (по базе)",
        await is_admin(_event(ADMIN_ID_2), cfg=cfg, db=db),
    )
    check("Реестр знает владельца", await admins.is_owner(ADMIN_ID))
    check("Обычный администратор — не владелец", not await admins.is_owner(ADMIN_ID_2))

    await db.set_admin_active(ADMIN_ID_2, False)
    admins.forget(ADMIN_ID_2)
    check(
        "Отключённый администратор теряет доступ",
        not await is_admin(_event(ADMIN_ID_2), admins=admins, cfg=cfg, db=db),
    )
    check(
        "Отключённого нет в списке активных",
        ADMIN_ID_2 not in await db.active_admin_ids(),
    )
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
        MULTI_CLIENT_ID,
        "VIP",
        "Сейчас (с 20:00, 23.10.2026)",
        "Мультиклиент",
        "AgAC_multi",
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
    check_equal(
        "Копии уведомлений сохранены в базе",
        len(await db.list_notifications(booking.id)),
        2,
    )

    # --- решение принимает второй администратор ------------------------------ #
    admin2_state = make_fsm_context(ADMIN_ID_2)
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
        admin2_state,
        db,
        bot,
        cfg,
    )
    decided = await db.get_booking(booking.id)
    check_equal(
        "Статус после решения второго админа", decided.status, BookingStatus.APPROVED
    )
    check_equal("В базе записано, кто подтвердил", decided.decided_by, ADMIN_ID_2)
    check("В базе записано, когда подтвердили", bool(decided.decided_at))

    client_notes = [
        payload
        for payload in bot.new_messages(mark)
        if getattr(payload, "chat_id", None) == MULTI_CLIENT_ID
    ]
    check(
        "Клиенту ушло уведомление о решении",
        bool(client_notes) and "бронь подтверждена" in text_of(client_notes[0]),
    )

    own_edit = [
        payload
        for name, payload in bot.since(mark)
        if name == "EditMessageCaption" and payload.chat_id == ADMIN_ID_2
    ]
    check("У принявшего решение подпись обновлена", bool(own_edit))
    check("В отметке видно «ПОДТВЕРЖДЕНО»", "ПОДТВЕРЖДЕНО" in text_of(own_edit[0]))
    check("В отметке назван администратор", "@second_admin" in text_of(own_edit[0]))
    check("Кнопки у принявшего решение убраны", markup_of(own_edit[0]) is None)

    peer_edit = [
        payload
        for name, payload in bot.since(mark)
        if name == "EditMessageCaption" and payload.chat_id == ADMIN_ID
    ]
    check("Копия владельца погашена", bool(peer_edit))
    check_equal(
        "Погашена именно та копия, что сохранена в базе",
        peer_edit[0].message_id,
        peer_message_id,
    )
    check(
        "В чужой копии сказано, что заявку обработали",
        "обработана" in text_of(peer_edit[0]),
    )
    check("В чужой копии названо действие", "подтвердил" in text_of(peer_edit[0]))
    check(
        "В чужой копии назван принявший решение",
        "@second_admin" in text_of(peer_edit[0]),
    )
    check("В чужой копии убраны кнопки", markup_of(peer_edit[0]) is None)
    check(
        "Повторная рассылка решения не трогает принявшего",
        await sync_peer_admins(bot, db, cfg, decided, exclude=ADMIN_ID_2) == [],
    )

    # --- гонка: два администратора жмут одновременно ------------------------- #
    race = await db.add_booking(
        MULTI_CLIENT_ID, "Double", "На ночь (22:00–08:00)", "Гонщик", "AgAC_race"
    )
    await db.add_notification(race.id, ADMIN_ID, 5001)
    await db.add_notification(race.id, ADMIN_ID_2, 5002)
    race_bot = BotSpy()
    race_bot.profiles.update(bot.profiles)
    race_state_1 = make_fsm_context(ADMIN_ID)
    race_state_2 = make_fsm_context(ADMIN_ID_2)
    await asyncio.gather(
        admin_handlers.cb_decision(
            make_callback(
                inline_keyboards.admin_approve_callback(race.id),
                user_id=ADMIN_ID,
                bot=race_bot,
                with_photo=True,
                caption="🔔",
            ),
            race_state_1,
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
            race_state_2,
            db,
            race_bot,
            cfg,
        ),
    )
    await race_state_1.storage.close()
    await race_state_2.storage.close()
    final = await db.get_booking(race.id)
    check(
        "При одновременном нажатии статус меняется один раз",
        final.status in (BookingStatus.APPROVED, BookingStatus.REJECTED),
    )
    check(
        "Решение записано за одним администратором",
        final.decided_by in (ADMIN_ID, ADMIN_ID_2),
    )
    check(
        "Клиенту при одновременном нажатии ушло одно решение",
        len([p for p in race_bot.select("SendMessage") if p.chat_id == MULTI_CLIENT_ID])
        == 1,
    )
    race_alerts = [
        payload
        for name, payload in race_bot.calls
        if name == "AnswerCallbackQuery" and getattr(payload, "show_alert", False)
    ]
    check("Проигравший администратор получил alert «уже обработана»", bool(race_alerts))
    check(
        "В alert назван тот, кто успел первым",
        any("Решение принял" in text_of(payload) for payload in race_alerts),
    )

    # --- панель: список и карточки администраторов --------------------------- #
    from bot.ui.screens import Screen, ScreenCB, nav

    panel_bot = BotSpy()
    panel_bot.profiles[ADMIN_ID_3] = SimpleNamespace(
        id=ADMIN_ID_3, username="new_admin", full_name="Новый"
    )
    panel_bot.profiles.update(bot.profiles)
    state = make_fsm_context(ADMIN_ID)
    mark = panel_bot.mark()
    await admin_handlers.cmd_admins(
        make_message(
            user_id=ADMIN_ID,
            bot=panel_bot,
            text="/admins",
            username="admin",
            first_name="Админ",
        ),
        state,
        db,
        cfg,
        admins,
    )
    listed = panel_bot.step_payloads(mark)[-1]
    check("Список администраторов отрисован", "АДМИНИСТРАТОРЫ" in text_of(listed))
    check(
        "В списке указано число записей и активных",
        "Записей: 2, активных: 2" in text_of(listed),
    )
    listed_btns = [b for row in markup_of(listed).inline_keyboard for b in row]
    check(
        "Каждый администратор — отдельная кнопка карточки",
        any(b.callback_data == nav(Screen.ADMIN_ADMIN, ADMIN_ID) for b in listed_btns),
    )
    check(
        "Владельцу доступна кнопка «➕ Добавить»",
        any(b.text == "➕ Добавить" for b in listed_btns),
    )

    # Карточка владельца из .env — без кнопок отключения/удаления
    mark = panel_bot.mark()
    await client_handlers.cb_navigate(
        make_callback(
            nav(Screen.ADMIN_ADMIN, ADMIN_ID), user_id=ADMIN_ID, bot=panel_bot
        ),
        ScreenCB(screen=Screen.ADMIN_ADMIN, arg=str(ADMIN_ID)),
        state,
        db,
        cfg,
        admins,
    )
    owner_card = panel_bot.step_payloads(mark)[-1]
    owner_card_text = text_of(owner_card)
    check(
        "В карточке показаны роль, статус и Telegram ID",
        all(
            part in owner_card_text
            for part in ("владелец", "🟢 активен", str(ADMIN_ID))
        ),
    )
    check(
        "Владелец из .env защищён — под ним нет кнопок управления",
        not any(
            b.callback_data and b.callback_data.startswith("ad:admin:")
            for row in markup_of(owner_card).inline_keyboard
            for b in row
        ),
    )

    # --- панель: добавление администратора ----------------------------------- #
    mark = panel_bot.mark()
    await admin_handlers.cb_admin_add_start(
        make_callback(inline_keyboards.CB_ADMIN_ADD, user_id=ADMIN_ID, bot=panel_bot),
        state,
        db,
        cfg,
        admins,
    )
    check_equal(
        "Включён режим ожидания ID", await state.get_state(), AdminFSM.waiting_admin_id
    )
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
        make_message(user_id=ADMIN_ID, bot=panel_bot, text="@nickname"),
        state,
        db,
        panel_bot,
        cfg,
        admins,
    )
    check(
        "Вместо ID — понятная подсказка",
        any("не похоже на Telegram ID" in t for t in panel_bot.step_texts(mark)),
    )
    check_equal(
        "Режим ввода не сброшен после ошибки",
        await state.get_state(),
        AdminFSM.waiting_admin_id,
    )
    check("Посторонний не добавился в базу", await db.get_admin(ADMIN_ID_3) is None)

    mark = panel_bot.mark()
    await admin_handlers.msg_admin_id(
        make_message(user_id=ADMIN_ID, bot=panel_bot, text=str(ADMIN_ID_3)),
        state,
        db,
        panel_bot,
        cfg,
        admins,
    )
    added = await db.get_admin(ADMIN_ID_3)
    check("Новый администратор записан в базу", added is not None and added.is_active)
    check_equal("Новый администратор — не владелец", added.role, AdminRole.OPERATOR)
    check_equal("Записано, кто добавил", added.added_by, ADMIN_ID)
    check_equal("Режим ввода сброшен", await state.get_state(), None)
    check("Реестр сразу знает нового администратора", await admins.is_admin(ADMIN_ID_3))
    check(
        "Предупредили, что новичку нужно нажать /start",
        any("/start" in t for t in panel_bot.step_texts(mark)),
    )
    check(
        "После добавления показан обновлённый список",
        any("АДМИНИСТРАТОРЫ" in t for t in panel_bot.step_texts(mark)),
    )

    # --- новый администратор получает доступ --------------------------------- #
    check(
        "Добавленный проходит фильтр IsAdmin",
        await IsAdmin()(_event(ADMIN_ID_3), admins=admins, cfg=cfg, db=db),
    )
    operator_state = make_fsm_context(ADMIN_ID_3)
    mark = panel_bot.mark()
    await admin_handlers.cmd_admin(
        make_message(
            user_id=ADMIN_ID_3,
            bot=panel_bot,
            text="/admin",
            username="new_admin",
            first_name="Новый",
        ),
        operator_state,
        db,
        cfg,
        admins,
    )
    check(
        "Новому администратору доступна статистика",
        any("Заявок всего" in t for t in panel_bot.step_texts(mark)),
    )

    # --- права: оператор не управляет списком -------------------------------- #
    mark = panel_bot.mark()
    await admin_handlers.cb_admin_add_start(
        make_callback(inline_keyboards.CB_ADMIN_ADD, user_id=ADMIN_ID_3, bot=panel_bot),
        operator_state,
        db,
        cfg,
        admins,
    )
    denies = [
        payload
        for name, payload in panel_bot.since(mark)
        if name == "AnswerCallbackQuery"
    ]
    check(
        "Оператору запрещено управлять списком",
        bool(denies) and denies[0].show_alert is True,
    )
    check("В отказе сказано про владельца", "только владелец" in text_of(denies[0]))
    check_equal(
        "Режим ввода оператору не включили", await operator_state.get_state(), None
    )

    mark = panel_bot.mark()
    await admin_handlers.cmd_admins(
        make_message(
            user_id=ADMIN_ID_3,
            bot=panel_bot,
            text="/admins",
            username="new_admin",
            first_name="Новый",
        ),
        operator_state,
        db,
        cfg,
        admins,
    )
    operator_view = panel_bot.step_payloads(mark)[-1]
    check(
        "Оператор видит список администраторов",
        "АДМИНИСТРАТОРЫ" in text_of(operator_view),
    )
    check(
        "Оператору объяснили отсутствие прав",
        "только владелец" in text_of(operator_view),
    )
    check(
        "У оператора нет кнопок управления",
        not any(
            b.callback_data and b.callback_data.startswith("ad:admin:")
            for row in markup_of(operator_view).inline_keyboard
            for b in row
        ),
    )

    # --- включение/отключение (открываем карточку и жмём кнопку) ------------- #
    await client_handlers.cb_navigate(
        make_callback(
            nav(Screen.ADMIN_ADMIN, ADMIN_ID_3), user_id=ADMIN_ID, bot=panel_bot
        ),
        ScreenCB(screen=Screen.ADMIN_ADMIN, arg=str(ADMIN_ID_3)),
        state,
        db,
        cfg,
        admins,
    )
    mark = panel_bot.mark()
    await admin_handlers.cb_admin_toggle(
        make_callback(
            inline_keyboards.admin_toggle_callback(ADMIN_ID_3),
            user_id=ADMIN_ID,
            bot=panel_bot,
        ),
        state,
        db,
        panel_bot,
        cfg,
        admins,
    )
    check(
        "Администратор отключён кнопкой", not (await db.get_admin(ADMIN_ID_3)).is_active
    )
    check("Отключённый потерял доступ", not await admins.is_admin(ADMIN_ID_3))
    check(
        "В карточке показан статус «отключён»",
        any("🔴 отключён" in t for t in panel_bot.step_texts(mark)),
    )
    check(
        "Об отключении сообщили",
        any("отключён" in text_of(p) for p in panel_bot.answers()[-1:]),
    )

    await admin_handlers.cb_admin_toggle(
        make_callback(
            inline_keyboards.admin_toggle_callback(ADMIN_ID_3),
            user_id=ADMIN_ID,
            bot=panel_bot,
        ),
        state,
        db,
        panel_bot,
        cfg,
        admins,
    )
    check("Администратор включён обратно", (await db.get_admin(ADMIN_ID_3)).is_active)
    check("Включённый снова проходит фильтр", await admins.is_admin(ADMIN_ID_3))

    # --- смена роли ---------------------------------------------------------- #
    await admin_handlers.cb_admin_role(
        make_callback(
            inline_keyboards.admin_promote_callback(ADMIN_ID_3),
            user_id=ADMIN_ID,
            bot=panel_bot,
        ),
        state,
        db,
        panel_bot,
        cfg,
        admins,
    )
    check_equal(
        "Роль повышена до владельца",
        (await db.get_admin(ADMIN_ID_3)).role,
        AdminRole.OWNER,
    )
    check("Новый владелец получил право управления", await admins.is_owner(ADMIN_ID_3))

    # --- защиты -------------------------------------------------------------- #
    mark = panel_bot.mark()
    await admin_handlers.cb_admin_delete(
        make_callback(
            inline_keyboards.admin_delete_callback(ADMIN_ID),
            user_id=ADMIN_ID,
            bot=panel_bot,
        ),
        state,
        db,
        panel_bot,
        cfg,
        admins,
    )
    protect = [
        payload
        for name, payload in panel_bot.since(mark)
        if name == "AnswerCallbackQuery"
    ]
    check(
        "Владельца из .env нельзя удалить",
        bool(protect) and "нельзя" in text_of(protect[0]),
    )
    check("Владелец из .env остался в базе", (await db.get_admin(ADMIN_ID)) is not None)

    mark = panel_bot.mark()
    await admin_handlers.cb_admin_delete(
        make_callback(
            inline_keyboards.admin_delete_callback(ADMIN_ID_3),
            user_id=ADMIN_ID_3,
            bot=panel_bot,
        ),
        operator_state,
        db,
        panel_bot,
        cfg,
        admins,
    )
    self_protect = [
        payload
        for name, payload in panel_bot.since(mark)
        if name == "AnswerCallbackQuery"
    ]
    check(
        "Себя удалить нельзя", bool(self_protect) and "Себя" in text_of(self_protect[0])
    )
    check("Самоудаление не тронуло базу", (await db.get_admin(ADMIN_ID_3)) is not None)

    await db.set_admin_active(ADMIN_ID, False)  # как если бы базу правили вручную
    admins.forget(ADMIN_ID)
    mark = panel_bot.mark()
    await admin_handlers.cb_admin_role(
        make_callback(
            inline_keyboards.admin_demote_callback(ADMIN_ID_3),
            user_id=ADMIN_ID_3,
            bot=panel_bot,
        ),
        operator_state,
        db,
        panel_bot,
        cfg,
        admins,
    )
    last_owner = [
        payload
        for name, payload in panel_bot.since(mark)
        if name == "AnswerCallbackQuery"
    ]
    check(
        "Последнего активного владельца нельзя понизить",
        bool(last_owner) and "последн" in text_of(last_owner[0]),
    )
    check_equal(
        "Роль последнего владельца не изменилась",
        (await db.get_admin(ADMIN_ID_3)).role,
        AdminRole.OWNER,
    )
    await db.set_admin_active(ADMIN_ID, True)
    admins.remember(ADMIN_ID)

    await admin_handlers.cb_admin_role(
        make_callback(
            inline_keyboards.admin_demote_callback(ADMIN_ID_3),
            user_id=ADMIN_ID,
            bot=panel_bot,
        ),
        state,
        db,
        panel_bot,
        cfg,
        admins,
    )
    check_equal(
        "Роль понижена, когда владельцев двое",
        (await db.get_admin(ADMIN_ID_3)).role,
        AdminRole.OPERATOR,
    )

    # --- отмена ввода ID ----------------------------------------------------- #
    cancel_state = make_fsm_context(ADMIN_ID)
    await cancel_state.set_state(AdminFSM.waiting_admin_id)
    mark = panel_bot.mark()
    await admin_handlers.cb_admin_add_cancel(
        make_callback(
            inline_keyboards.CB_ADMIN_ADD_CANCEL, user_id=ADMIN_ID, bot=panel_bot
        ),
        cancel_state,
        db,
        cfg,
        admins,
    )
    check_equal("Отмена сбрасывает режим ввода", await cancel_state.get_state(), None)
    check(
        "Об отмене сообщили", any("отменено" in t for t in panel_bot.step_texts(mark))
    )

    # --- удаление ------------------------------------------------------------ #
    mark = panel_bot.mark()
    await admin_handlers.cb_admin_delete(
        make_callback(
            inline_keyboards.admin_delete_callback(ADMIN_ID_3),
            user_id=ADMIN_ID,
            bot=panel_bot,
        ),
        state,
        db,
        panel_bot,
        cfg,
        admins,
    )
    check("Администратор удалён из базы", (await db.get_admin(ADMIN_ID_3)) is None)
    check("Удалённый потерял доступ", not await admins.is_admin(ADMIN_ID_3))
    check(
        "Об удалении сообщили",
        any("удален" in text_of(p) for p in panel_bot.answers()[-1:]),
    )

    # --- статистика и кнопка «🛠 Админ-панель» в меню ------------------------ #
    stats = await db.get_stats()
    check(
        "Статистика считает администраторов",
        stats.admins == 2 and stats.admins_active == 2,
    )
    mark = panel_bot.mark()
    await admin_handlers.cmd_admin(
        make_message(
            user_id=ADMIN_ID,
            bot=panel_bot,
            text="/admin",
            username="admin",
            first_name="Админ",
        ),
        state,
        db,
        cfg,
        admins,
    )
    panel_payload = panel_bot.step_payloads(mark)[-1]
    check(
        "В статистике панели видно администраторов",
        "Администраторов" in text_of(panel_payload),
    )
    check(
        "В панели есть кнопка «👥 Администраторы»",
        any(
            b.text == "👥 Администраторы"
            for row in markup_of(panel_payload).inline_keyboard
            for b in row
        ),
    )

    mark = panel_bot.mark()
    admin_state = make_fsm_context(ADMIN_ID_2)
    await db.upsert_user(ADMIN_ID_2, "second_admin", "Второй")
    await db.set_consent(ADMIN_ID_2, granted=True)
    await client_handlers.cmd_start(
        make_message(
            user_id=ADMIN_ID_2,
            bot=panel_bot,
            text="/start",
            username="second_admin",
            first_name="Второй",
        ),
        admin_state,
        db,
        cfg,
        admins,
    )
    greeting = panel_bot.step_texts(mark)
    admin_menu_payload = [
        p
        for p in panel_bot.step_payloads(mark)
        if getattr(markup_of(p), "inline_keyboard", None)
    ][-1]
    admin_menu_btns = [
        b.text for row in markup_of(admin_menu_payload).inline_keyboard for b in row
    ]
    check(
        "У администратора в меню есть кнопка «🛠 Админ-панель»",
        "🛠 Админ-панель" in admin_menu_btns,
    )
    check_equal(
        "Администратору /start отправляет ровно один фото-экран без второго сообщения",
        len(panel_bot.new_messages(mark)),
        1,
    )
    check(
        "В подписи меню администратора отмечен активный доступ",
        any("Доступ администратора активен" in t for t in greeting),
    )
    check("В подписи меню названа роль", any("администратор" in t for t in greeting))

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
    guest_menu_payload = [
        p
        for p in panel_bot.step_payloads(mark)
        if getattr(markup_of(p), "inline_keyboard", None)
    ][-1]
    guest_menu_btns = [
        b.text for row in markup_of(guest_menu_payload).inline_keyboard for b in row
    ]
    check(
        "У обычного гостя кнопки «🛠 Админ-панель» нет",
        "🛠 Админ-панель" not in guest_menu_btns,
    )
    check(
        "Клиенту пометка администратора не показывается",
        not any(
            "Доступ администратора активен" in t for t in panel_bot.step_texts(mark)
        ),
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
        any("только для администраторов" in t for t in panel_bot.new_texts(mark)),
    )

    await db.set_admin_active(ADMIN_ID_2, False)
    mark = panel_bot.mark()
    await client_handlers.cmd_admin_no_access(
        make_message(
            user_id=ADMIN_ID_2,
            bot=panel_bot,
            text="/admin",
            username="second_admin",
            first_name="Второй",
        ),
        db,
    )
    check(
        "Отключённому администратору сообщают об отключении",
        any("отключён" in t and "панель" in t for t in panel_bot.new_texts(mark)),
    )
    await db.set_admin_active(ADMIN_ID_2, True)
    admins.remember(ADMIN_ID_2)

    for context in (
        admin2_state,
        state,
        operator_state,
        cancel_state,
        admin_state,
        client_state,
    ):
        await context.storage.close()


# --------------------------------------------------------------------------- #
# 13. Оплата: чеком или на месте (реальные правила клуба)
# --------------------------------------------------------------------------- #
async def test_payment_choice(db: Database, cfg: AppConfig) -> None:
    """Ночь в ПТ–ВС — только по предоплате, в остальных случаях можно на месте."""
    print("\n— Способ оплаты: чек или на месте —\n")
    bot = BotSpy()

    # --- ночь в выходной: кнопки «на месте» нет, предоплата обязательна ----- #
    night_keyboard = inline_keyboards.payment_choice_keyboard(True)
    night_buttons = [b.text for row in night_keyboard.inline_keyboard for b in row]
    check(
        "Для ночи в ПТ–ВС доступен только чек",
        sum("чек" in t for t in night_buttons) == 1
        and not any("на месте" in t for t in night_buttons),
        str(night_buttons),
    )

    # --- оплата на месте: заявка создаётся без чека ------------------------- #
    client_id = 424_242_424
    await db.upsert_user(client_id, "spot_client", "Гость")
    await db.set_consent(client_id, granted=True)
    state = make_fsm_context(client_id)

    await state.set_state(BookingFSM.choosing_payment)
    await state.update_data(
        {
            DATA_TARIFF_KEY: "standart",
            DATA_TIME_KEY: texts.TIME_NOW_KEY,
            DATA_TIME_SLOT: "Сейчас (с 19:00, 23.10.2026)",
            DATA_CLIENT_NAME: "Гость",
        }
    )

    mark = bot.mark()
    await client_handlers.cb_choose_payment(
        make_callback(inline_keyboards.CB_PAY_SPOT, user_id=client_id, bot=bot),
        state,
        db,
        cfg,
    )
    check_equal("После «на месте» FSM очищен", await state.get_state(), None)

    spot_texts = bot.step_texts(mark)
    check(
        "Клиенту подтверждена заявка без чека",
        any("ЗАЯВКА ОТПРАВЛЕНА" in t for t in spot_texts),
    )
    check(
        "В подтверждении сказано про оплату на месте",
        any("на месте" in t for t in spot_texts),
    )
    spot_client_payload = [
        p for p in bot.step_payloads(mark) if getattr(p, "chat_id", None) == client_id
    ][-1]
    check(
        "Под подтверждением есть кнопки возврата в меню",
        any(
            b.text == "🏠 В меню"
            for row in markup_of(spot_client_payload).inline_keyboard
            for b in row
        ),
    )

    spot_bookings = [
        b
        for b in await db.list_bookings(BookingStatus.PENDING, limit=50)
        if b.client_name == "Гость"
    ]
    check_equal("Заявка «на месте» сохранена", len(spot_bookings), 1)
    spot_booking = spot_bookings[0]
    check("В заявке «на месте» нет чека", spot_booking.receipt_file_id is None)

    admin_messages = [
        p
        for n, p in bot.since(mark)
        if n in ("SendPhoto", "SendMessage") and getattr(p, "chat_id", None) == ADMIN_ID
    ]
    check("Администратор уведомлён о заявке «на месте»", bool(admin_messages))
    admin_caption = text_of(admin_messages[0])
    check("В уведомлении сказано про оплату на месте", "на месте" in admin_caption)
    check("В уведомлении НЕ обещан чек", "Чек об оплате — на фото" not in admin_caption)
    check("В уведомлении есть строка «К оплате»", "К оплате" in admin_caption)
    check(
        "Под заявкой «на месте» есть кнопки решения",
        markup_of(admin_messages[0]) is not None
        and any(
            inline_keyboards.parse_admin_decision(b.callback_data) is not None
            for row in markup_of(admin_messages[0]).inline_keyboard
            for b in row
        ),
    )

    # --- «на месте» для ночи в выходные: отказ с понятным alert ------------- #
    # Имитируем пятницу/субботу, подменив определение выходного дня.
    original_is_weekend = texts.is_weekend
    texts.is_weekend = lambda _day: True  # type: ignore[assignment]
    try:
        weekend_state = make_fsm_context(client_id)
        await weekend_state.set_state(BookingFSM.choosing_payment)
        await weekend_state.update_data(
            {
                DATA_TARIFF_KEY: "standart",
                DATA_TIME_KEY: texts.TIME_NIGHT_KEY,
                DATA_TIME_SLOT: "Ночь (22:00–08:00)",
                DATA_CLIENT_NAME: "Гость",
            }
        )
        mark = bot.mark()
        await client_handlers.cb_choose_payment(
            make_callback(inline_keyboards.CB_PAY_SPOT, user_id=client_id, bot=bot),
            weekend_state,
            db,
            cfg,
        )
        alerts = [p for n, p in bot.since(mark) if n == "AnswerCallbackQuery"]
        check(
            "Ночь в ПТ–ВС нельзя оплатить на месте",
            bool(alerts) and alerts[0].show_alert is True,
        )
        check(
            "В alert объяснена причина",
            "предоплате" in text_of(alerts[0]),
            text_of(alerts[0]) if alerts else "нет alert",
        )
        check_equal(
            "Режим оплаты не сброшен",
            await weekend_state.get_state(),
            BookingFSM.choosing_payment.state,
        )
        await weekend_state.storage.close()
    finally:
        texts.is_weekend = original_is_weekend  # type: ignore[assignment]

    # --- выбор чека: бот уходит в ожидание фото ----------------------------- #
    check_state = make_fsm_context(client_id)
    await check_state.set_state(BookingFSM.choosing_payment)
    await check_state.update_data(
        {
            DATA_TARIFF_KEY: "standart",
            DATA_TIME_KEY: texts.TIME_NIGHT_KEY,
            DATA_TIME_SLOT: "Ночь (22:00–08:00)",
            DATA_CLIENT_NAME: "Гость",
        }
    )
    await client_handlers.cb_choose_payment(
        make_callback(inline_keyboards.CB_PAY_CHECK, user_id=client_id, bot=bot),
        check_state,
        db,
        cfg,
    )
    check_equal(
        "Выбор чека → ожидание фото",
        await check_state.get_state(),
        BookingFSM.waiting_receipt.state,
    )

    await state.storage.close()
    await check_state.storage.close()


# --------------------------------------------------------------------------- #
# 14. Интерфейс одного экрана (HUD): переходы, стек «Назад», нет мусора в чате
# --------------------------------------------------------------------------- #
async def test_single_screen_hud(db: Database, dispatcher: Dispatcher) -> None:
    """Проверить, что вся прогулка по разделам правит ОДНО сообщение."""
    from bot.ui.screens import SCREEN_BACK, SCREEN_TITLES, Screen, nav

    print("\n— Одноэкранный HUD-интерфейс —")
    bot = BotSpy()

    guest_id = 606_707_808
    await db.upsert_user(guest_id, "hud_guest", "Игрок")
    await db.set_consent(guest_id, granted=True)
    await db.upsert_user(ADMIN_ID, "hud_owner", "Владелец")
    await db.set_consent(ADMIN_ID, granted=True)

    seq = 0

    async def send(event: Any) -> int:
        nonlocal seq
        seq += 1
        mark = bot.mark()
        await dispatcher.feed_update(bot, make_update(seq, event))
        return mark

    # 1. /start создаёт ровно одно фото-сообщение экрана
    m = await send(make_message(user_id=guest_id, bot=bot, text="/start"))
    created_photos = [p for n, p in bot.since(m) if n == "SendPhoto"]
    created_texts = [p for n, p in bot.since(m) if n == "SendMessage"]
    check_equal("HUD: /start отправляет ровно один фото-экран", len(created_photos), 1)
    check_equal("HUD: /start не шлёт лишних текстовых сообщений", len(created_texts), 0)
    screen_msg_id = bot._message_ids

    def cb(data: str, uid: int = guest_id, mid: int = screen_msg_id) -> CallbackQuery:
        src = make_message(user_id=uid, bot=bot, message_id=mid)
        return CallbackQuery(
            id=f"hud_{abs(hash((data, seq))) % 10**8}",
            from_user=src.from_user,
            chat_instance="hud",
            message=src,
            data=data,
        ).as_(bot)

    # 2. 12 переходов подряд (включая вкладки «Правил») → 0 новых сообщений, 12 EditMessageMedia
    walk = [
        nav(Screen.PRICES),
        nav(Screen.FAQ),
        nav(Screen.FAQ_ITEM, "night"),
        nav(Screen.FAQ_ITEM, "prepay"),
        nav(SCREEN_BACK),
        nav(SCREEN_BACK),
        nav(Screen.CONTACTS),
        nav(Screen.RULES),
        nav(Screen.RULES, "pay"),
        nav(Screen.RULES, "club"),
        nav(SCREEN_BACK),
        nav(Screen.MENU),
    ]
    mark = bot.mark()
    for step in walk:
        await send(cb(step))
    sends_during_walk = [
        p for n, p in bot.since(mark) if n in ("SendMessage", "SendPhoto")
    ]
    edits_during_walk = [p for n, p in bot.since(mark) if n == "EditMessageMedia"]
    check_equal(
        "HUD: за 12 переходов 0 новых сообщений в чате", len(sends_during_walk), 0
    )
    check_equal(
        "HUD: все 12 переходов обновили тот же медиа-экран", len(edits_during_walk), 12
    )
    check(
        "HUD: правился один и тот же message_id",
        {p.message_id for p in edits_during_walk} == {screen_msg_id},
    )
    # Повторный вход в главное меню использует кэшированный file_id, а не байты
    check(
        "HUD: повторный показ карточки берёт закэшированный file_id",
        isinstance(edits_during_walk[-1].media.media, str)
        and edits_during_walk[-1].media.media.startswith("AgAC_card_"),
    )

    # 3. Все заголовки экранов покрыты
    check("HUD: у каждого экрана есть заголовок", len(SCREEN_TITLES) >= 20)

    # 4. У гостя в меню нет админки, у администратора — есть (и без второго сообщения!)
    guest_last = edits_during_walk[-1]
    guest_btns = [b.text for r in markup_of(guest_last).inline_keyboard for b in r]
    check("HUD: гость не видит кнопку админ-панели", "🛠 Админ-панель" not in guest_btns)

    m = await send(
        make_message(
            user_id=ADMIN_ID,
            bot=bot,
            text="/start",
            username="hud_owner",
            first_name="Владелец",
        )
    )
    check_equal(
        "HUD: админ при /start получает ровно 1 сообщение (без спама ниже меню)",
        len([p for n, p in bot.since(m) if n in ("SendMessage", "SendPhoto")]),
        1,
    )
    admin_screen = [
        p
        for p in bot.step_payloads(m)
        if getattr(markup_of(p), "inline_keyboard", None)
    ][-1]
    admin_mid = getattr(admin_screen, "message_id", None) or bot._message_ids
    admin_btns = [b.text for r in markup_of(admin_screen).inline_keyboard for b in r]
    check(
        "HUD: администратор видит «🛠 Админ-панель» в клавиатуре меню",
        "🛠 Админ-панель" in admin_btns,
    )

    m = await send(cb(nav(Screen.ADMIN), uid=ADMIN_ID, mid=admin_mid))
    check(
        "HUD: нажатие «🛠 Админ-панель» открывает сводку",
        any("АДМИН-ПАНЕЛЬ" in t for t in bot.step_texts(m)),
    )
    m = await send(cb(nav(SCREEN_BACK), uid=ADMIN_ID, mid=admin_mid))
    check(
        "HUD: «◀ Назад» из админ-панели возвращает в главное меню",
        any("ГЛАВНОЕ МЕНЮ" in t for t in bot.step_texts(m)),
    )


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
    workdir = Path(tempfile.mkdtemp(prefix="nova-migrate-"))
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
        check(
            "В старую базу добавлена таблица booking_notifications",
            "booking_notifications" in tables,
        )
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


async def test_graphical_cards(db: Database, cfg: AppConfig) -> None:
    """Проверить графические карточки (1280×720), отсутствие <pre> и лимит 1024."""
    from io import BytesIO

    from PIL import Image

    from bot.ui.render import CAPTION_LIMIT, ViewContext, render_screen
    from bot.ui.screens import Screen
    from bot.utils.formatting import truncate_html, visible_length

    print("\n— Графические карточки (Pillow 1280×720), типографика и лимит 1024 —")
    bot = BotSpy()
    state = make_fsm_context(ADMIN_ID)
    await state.update_data(
        **{
            DATA_TARIFF_KEY: "svip",
            DATA_TIME_KEY: "night",
            DATA_TIME_SLOT: "На ночь (22:00–08:00)",
            DATA_CLIENT_NAME: "Александр",
        }
    )
    sample_booking = await db.add_booking(
        ADMIN_ID, "S VIP", "На ночь (22:00–08:00)", "Александр", "AgAC_ticket_demo"
    )
    ctx = ViewContext(
        db=db,
        cfg=cfg,
        bot=bot,
        user_id=ADMIN_ID,
        state=state,
        admins=AdminRegistry(db, cfg),
    )

    screens_to_check: list[tuple[str, str]] = [
        (Screen.MENU, ""),
        (Screen.PRICES, ""),
        (Screen.RULES, "main"),
        (Screen.RULES, "pay"),
        (Screen.RULES, "club"),
        (Screen.FAQ, ""),
        *[(Screen.FAQ_ITEM, item.key) for item in texts.FAQ_ITEMS],
        (Screen.CONTACTS, ""),
        (Screen.BOOKINGS, ""),
        (Screen.BOOK_TARIFF, ""),
        (Screen.BOOK_TIME, ""),
        (Screen.BOOK_TIME_TEXT, ""),
        (Screen.BOOK_NAME, ""),
        (Screen.BOOK_PAY, ""),
        (Screen.BOOK_RECEIPT, ""),
        (Screen.BOOK_DONE, str(sample_booking.id)),
        (Screen.CONSENT, ""),
        (Screen.PRIVACY, ""),
        (Screen.PRIVACY_CONFIRM, ""),
        (Screen.CONSENT_REVOKED, "24.10.2026 12:00"),
        (Screen.ADMIN, ""),
        (Screen.ADMIN_PENDING, "0"),
        (Screen.ADMIN_ADMINS, ""),
        (Screen.ADMIN_ADMIN, str(ADMIN_ID)),
        (Screen.ADMIN_ADD, ""),
    ]

    for scr, arg in screens_to_check:
        label = f"{scr}:{arg}" if arg else scr
        view = await render_screen(ctx, scr, arg)
        with Image.open(BytesIO(view.card.jpeg_bytes)) as img:
            size_ok = img.size == (1280, 720) and img.mode == "RGB"
        vis_len = visible_length(view.caption)
        no_pre = "<pre>" not in view.caption and "</pre>" not in view.caption
        no_meta = "он откроется здесь же" not in view.caption
        check(
            f"Экран [{label}]: JPEG 1280×720, без <pre>, подпись {vis_len} ≤ {CAPTION_LIMIT}",
            size_ok and no_pre and no_meta and 0 < vis_len <= CAPTION_LIMIT,
            f"size_ok={size_ok}, no_pre={no_pre}, vis_len={vis_len}",
        )

    # Проверка безопасной обрезки HTML с закрытием тегов
    long_html = (
        "<blockquote><b>Заголовок</b> — "
        + ("текст &amp; символы " * 80)
        + "</blockquote>"
    )
    cut = truncate_html(long_html, 120)
    check(
        "truncate_html укладывается в лимит и закрывает HTML-теги",
        visible_length(cut) <= 120
        and cut.endswith("</blockquote>")
        and cut.count("<blockquote>") == cut.count("</blockquote>"),
    )

    # Проверка многоцветных inline-кнопок Bot API и CopyTextButton
    menu_view = await render_screen(ctx, Screen.MENU, "")
    hero_btn = menu_view.reply_markup.inline_keyboard[0][0]
    check_equal("Главная CTA-кнопка меню окрашена в success", hero_btn.style, "success")
    menu_styles = {
        b.style
        for row in menu_view.reply_markup.inline_keyboard
        for b in row
        if getattr(b, "style", None)
    }
    check(
        "Главное меню содержит все три цвета Bot API (success, primary, danger)",
        {"success", "primary", "danger"}.issubset(menu_styles),
        f"menu_styles={menu_styles}",
    )

    tariff_view = await render_screen(ctx, Screen.BOOK_TARIFF, "")
    tariff_styles = [
        b.style
        for row in tariff_view.reply_markup.inline_keyboard
        for b in row
        if getattr(b, "style", None)
    ]
    check("Кнопки выбора зала раскрашены акцентами Bot API", len(tariff_styles) >= 5)

    contacts_view = await render_screen(ctx, Screen.CONTACTS, "")
    copy_btns = [
        b
        for row in contacts_view.reply_markup.inline_keyboard
        for b in row
        if getattr(b, "copy_text", None) is not None
    ]
    check(
        "На экране контактов есть цветные кнопки копирования реквизитов и телефона",
        len(copy_btns) == 2
        and all(getattr(b, "style", None) in ("success", "primary") for b in copy_btns),
    )

    # Проверка полного отсутствия данных реального клуба в демо-сборке
    env_example_txt = (
        Path(__file__).resolve().parent.parent / ".env.example"
    ).read_text(encoding="utf-8")
    forbidden_real_tokens = (
        "ADMIRAL",
        "Чита",
        "Подгорбунск",
        "9143618141",
        "299-35-35",
        "Тимур",
        "Сбербанк",
    )
    default_dump = " ".join(
        [
            *ClubInfo().as_dict().values(),
            env_example_txt,
            menu_view.caption,
            contacts_view.caption,
        ]
    )
    check(
        "В демо-настройках и подписях нет ни одного упоминания реального клуба",
        all(tok.lower() not in default_dump.lower() for tok in forbidden_real_tokens),
    )

    # Проверка нового логотипа демо-бота NOVA ARENA, эмблемы карточек и фонов
    from bot.ui.cards import ASSETS_DIR, BG_DIR, LOGO_PATH

    bot_logo_path = ASSETS_DIR / "nova_arena_bot_logo.png"
    check(
        "Отдельный логотип самого бота bot/assets/nova_arena_bot_logo.png существует",
        bot_logo_path.is_file(),
    )
    with Image.open(bot_logo_path) as bot_logo_img:
        check(
            "Логотип самого бота квадратный высокого разрешения (>= 1024×1024)",
            bot_logo_img.width == bot_logo_img.height and bot_logo_img.width >= 1024,
        )

    check(
        "Фирменный логотип карточек bot/assets/logo.png существует", LOGO_PATH.is_file()
    )
    with Image.open(LOGO_PATH) as logo_img:
        check(
            "Логотип карточек прозрачный по краям (RGBA без чёрной рамки)",
            logo_img.mode == "RGBA" and logo_img.getpixel((0, 0))[3] == 0,
        )
    check(
        "Аватар демо-клуба bot/assets/avatar.jpg подготовлен для @BotFather",
        (ASSETS_DIR / "avatar.jpg").is_file(),
    )
    for bg_name in ("lounge", "vip", "pass", "admin"):
        bg_file = BG_DIR / f"{bg_name}.jpg"
        with Image.open(bg_file) as bg_img:
            check(
                f"Абстрактный фон {bg_name}.jpg имеет размер 1280×720",
                bg_img.size == (1280, 720),
            )

    # Проверка About, Description и Description Picture (640×360 для @BotFather)
    about_default = texts.build_bot_about(ClubInfo())
    desc_default = texts.build_bot_description(ClubInfo())
    long_contact_club = ClubInfo(bot_contact="@" + "x" * 32)
    about_long = texts.build_bot_about(long_contact_club)
    desc_long = texts.build_bot_description(long_contact_club)
    check(
        "About бота укладывается в 120 символов и содержит контакт",
        0 < len(about_default) <= texts.BOT_ABOUT_LIMIT
        and len(about_long) <= texts.BOT_ABOUT_LIMIT
        and "@ваш_ник" in about_default
        and "{" not in about_default,
        f"len={len(about_default)}, long_len={len(about_long)}",
    )
    check(
        "Description бота укладывается в 512 символов и содержит все тезисы",
        0 < len(desc_default) <= texts.BOT_DESCRIPTION_LIMIT
        and len(desc_long) <= texts.BOT_DESCRIPTION_LIMIT
        and all(
            phrase in desc_default
            for phrase in (
                "убирает бронь из общего чата",
                "Google Sheets, IZI",
                "@ваш_ник",
                "Подтвердить",
            )
        )
        and "{" not in desc_default,
        f"len={len(desc_default)}, long_len={len(desc_long)}",
    )
    for pic_name in ("description_pic.png", "description_pic.jpg"):
        pic_file = ASSETS_DIR / pic_name
        check(f"Description Picture {pic_name} существует", pic_file.is_file())
        with Image.open(pic_file) as pic_img:
            check(
                f"Description Picture {pic_name} ровно 640×360 для @BotFather",
                pic_img.size == (640, 360),
            )
    hd_pic_file = ASSETS_DIR / "description_pic_hd.png"
    with Image.open(hd_pic_file) as hd_img:
        check("HD-версия Description Picture имеет размер 1280×720", hd_img.size == (1280, 720))

    await state.storage.close()


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #
async def run_all() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="nova-db-"))
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
        check(
            "Схема применяется повторно без потери данных",
            (await db.get_stats()).users >= 1,
        )

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
        await test_single_screen_hud(db, dispatcher)
        await dispatcher.storage.close()

        await test_multi_admin(db)
        await test_payment_choice(db, cfg)
        await test_graphical_cards(db, cfg)
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
