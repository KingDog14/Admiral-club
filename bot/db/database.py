"""Репозиторий SQLite поверх :mod:`aiosqlite`.

Особенности реализации:

* одно соединение на процесс, все операции сериализуются через
  :class:`asyncio.Lock` — SQLite не любит параллельной записи;
* ``PRAGMA journal_mode=WAL`` — чтение не блокируется записью;
* ``created_at`` пишется в ISO-8601 с часовым поясом клуба;
* схема создаётся автоматически из ``schema.sql``, а старые базы прозрачно
  дополняются новыми столбцами и стартовыми справочниками в :meth:`Database._migrate`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from sqlite3 import Row
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from bot.config import (
    DEFAULT_ADMIN_PHONE,
    DEFAULT_CLUB_ADDRESS,
    DEFAULT_CLUB_CITY,
    DEFAULT_CLUB_HOURS,
    DEFAULT_CLUB_NAME,
)
from bot.db.models import (
    Admin,
    AdminInvite,
    AdminRole,
    Booking,
    BookingStats,
    BookingStatus,
    Branch,
    ExternalBusySlot,
    RejectReason,
    ScheduleDay,
    ScheduleHoliday,
    TariffExtra,
    TariffRecord,
    User,
)

logger = logging.getLogger(__name__)

SCHEMA_FILE: Path = Path(__file__).with_name("schema.sql")

#: Недостающие столбцы таблицы bookings для мягкой миграции старых баз.
BOOKINGS_MIGRATIONS: dict[str, str] = {
    "decided_by": "ALTER TABLE bookings ADD COLUMN decided_by INTEGER",
    "decided_at": "ALTER TABLE bookings ADD COLUMN decided_at DATETIME",
    "branch_id": "ALTER TABLE bookings ADD COLUMN branch_id INTEGER DEFAULT 1",
    "booking_date": "ALTER TABLE bookings ADD COLUMN booking_date TEXT",
    "slot_start": "ALTER TABLE bookings ADD COLUMN slot_start TEXT",
    "slot_end": "ALTER TABLE bookings ADD COLUMN slot_end TEXT",
    "guests_count": "ALTER TABLE bookings ADD COLUMN guests_count INTEGER NOT NULL DEFAULT 1",
    "seat_label": "ALTER TABLE bookings ADD COLUMN seat_label TEXT",
    "guest_comment": "ALTER TABLE bookings ADD COLUMN guest_comment TEXT",
    "guest_contact": "ALTER TABLE bookings ADD COLUMN guest_contact TEXT",
    "extras_json": "ALTER TABLE bookings ADD COLUMN extras_json TEXT NOT NULL DEFAULT '[]'",
    "quoted_price": "ALTER TABLE bookings ADD COLUMN quoted_price INTEGER",
    "receipt_kind": "ALTER TABLE bookings ADD COLUMN receipt_kind TEXT NOT NULL DEFAULT 'photo'",
    "reject_reason": "ALTER TABLE bookings ADD COLUMN reject_reason TEXT",
    "reject_comment": "ALTER TABLE bookings ADD COLUMN reject_comment TEXT",
    "was_approved": "ALTER TABLE bookings ADD COLUMN was_approved BOOLEAN NOT NULL DEFAULT 0",
    "no_show": "ALTER TABLE bookings ADD COLUMN no_show BOOLEAN NOT NULL DEFAULT 0",
    "created_by_admin": "ALTER TABLE bookings ADD COLUMN created_by_admin BOOLEAN NOT NULL DEFAULT 0",
}

#: Недостающие столбцы таблицы admins для мягкой миграции старых баз.
ADMINS_MIGRATIONS: dict[str, str] = {
    "display_name": "ALTER TABLE admins ADD COLUMN display_name TEXT",
    "branch_id": "ALTER TABLE admins ADD COLUMN branch_id INTEGER",
    "notify_new": "ALTER TABLE admins ADD COLUMN notify_new BOOLEAN NOT NULL DEFAULT 1",
    "notify_cancel": "ALTER TABLE admins ADD COLUMN notify_cancel BOOLEAN NOT NULL DEFAULT 1",
    "notify_noshow": "ALTER TABLE admins ADD COLUMN notify_noshow BOOLEAN NOT NULL DEFAULT 1",
    "quiet_start": "ALTER TABLE admins ADD COLUMN quiet_start TEXT",
    "quiet_end": "ALTER TABLE admins ADD COLUMN quiet_end TEXT",
}

#: Индексы, создаваемые после миграции столбцов.
POST_MIGRATION_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_bookings_decided ON bookings (decided_by)",
    "CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings (booking_date, branch_id, status)",
)

#: Стартовые тарифы по умолчанию (совпадают с демо-тарифами в bot/texts.py).
_DEFAULT_TARIFFS: tuple[tuple[Any, ...], ...] = (
    (
        "standart",
        "Standart",
        "🟢",
        16,
        120,
        140,
        320,
        360,
        500,
        560,
        350,
        450,
        550,
        650,
        "Общий зал, 16 ПК",
        1,
    ),
    (
        "vip",
        "VIP",
        "🟡",
        10,
        170,
        190,
        460,
        510,
        700,
        780,
        480,
        620,
        750,
        850,
        "VIP-зал, 10 ПК",
        2,
    ),
    (
        "svip",
        "S VIP",
        "🟠",
        5,
        230,
        260,
        620,
        690,
        950,
        1050,
        650,
        800,
        980,
        1100,
        "Буткемп S VIP, 5 ПК",
        3,
    ),
    (
        "double",
        "Double",
        "💎",
        2,
        280,
        320,
        750,
        840,
        1150,
        1280,
        780,
        950,
        1150,
        1300,
        "Приватная парная комната, 2 ПК",
        4,
    ),
)

#: Стартовые дополнительные услуги.
_DEFAULT_EXTRAS: tuple[tuple[str, str, str, int, int], ...] = (
    ("own_pc", "Свой ПК / периферия", "🖥", 0, 1),
    ("vip_room", "Апгрейд VIP-зал", "👑", 150, 1),
    ("drink", "Напиток / энергетик", "🥤", 180, 1),
)

#: Стартовые настройки расписания и правил клуба.
_DEFAULT_CLUB_SETTINGS: dict[str, str] = {
    "slot_step_minutes": "60",
    "slot_max_guests": "16",
    "slot_buffer_minutes": "15",
    "booking_rules_lead_hours": "0",
    "booking_rules_max_days": "14",
    "booking_rules_allow_cancel": "1",
    "booking_rules_noshow_penalty": (
        "Ожидание гостя — 15 минут. При неявке без предупреждения бронь снимается, "
        "предоплата ночного пакета не возвращается."
    ),
    "chat_url": "",
    "welcome_text": "",
    "payment_text": "",
    "payment_photo_file_id": "",
}

_UNSET = object()


class Database:
    """Асинхронный доступ к базе данных бота."""

    def __init__(self, path: str | Path, *, timezone: ZoneInfo) -> None:
        self._path = Path(path)
        self._timezone = timezone
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def timezone(self) -> ZoneInfo:
        return self._timezone

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError(
                f"База данных {self._path} не подключена: сначала вызовите await db.connect()"
            )
        return self._connection

    async def connect(self) -> None:
        """Открыть соединение, включить PRAGMA, применить схему, миграции и справочники."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self._path)
        self._connection.row_factory = aiosqlite.Row

        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA synchronous=NORMAL")
        await self._connection.execute("PRAGMA foreign_keys=ON")
        await self._connection.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        await self._connection.commit()
        await self._migrate()
        await self._seed_defaults()

        logger.info("База данных готова: %s", self._path)

    async def close(self) -> None:
        """Закрыть соединение (вызывается при остановке бота)."""
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
            logger.info("Соединение с базой данных закрыто")

    def now(self) -> str:
        """Текущее время клуба в ISO-8601."""
        return datetime.now(self._timezone).isoformat(timespec="seconds")

    def today_iso(self) -> str:
        """Текущая дата клуба в формате YYYY-MM-DD."""
        return datetime.now(self._timezone).date().isoformat()

    async def _table_columns(self, table: str) -> set[str]:
        if not table.isidentifier():
            raise ValueError(f"Некорректное имя таблицы: {table!r}")
        async with (
            self._lock,
            self.connection.execute(f"PRAGMA table_info({table})") as cursor,
        ):
            rows = await cursor.fetchall()
        return {str(row["name"]) for row in rows}

    async def _migrate(self) -> None:
        """Добавить недостающие столбцы и обновить CHECK ролей в старых базах."""
        booking_cols = await self._table_columns("bookings")
        for column, ddl in BOOKINGS_MIGRATIONS.items():
            if column not in booking_cols:
                await self._execute(ddl)
                logger.info("Миграция: в таблицу bookings добавлен столбец %s", column)

        admin_cols = await self._table_columns("admins")
        for column, ddl in ADMINS_MIGRATIONS.items():
            if column not in admin_cols:
                await self._execute(ddl)
                logger.info("Миграция: в таблицу admins добавлен столбец %s", column)

        # Если таблица admins была создана старой версией без роли 'viewer' в CHECK — обновим её.
        row = await self._fetch_one(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'admins'"
        )
        if row is not None and row["sql"] and "viewer" not in str(row["sql"]):
            async with self._lock:
                await self.connection.executescript(
                    """
                    ALTER TABLE admins RENAME TO _admins_legacy;
                    CREATE TABLE admins (
                        user_id       INTEGER PRIMARY KEY,
                        role          TEXT    NOT NULL DEFAULT 'operator'
                                      CHECK (role IN ('owner', 'operator', 'admin', 'viewer')),
                        is_active     BOOLEAN NOT NULL DEFAULT 1,
                        added_by      INTEGER,
                        created_at    DATETIME,
                        display_name  TEXT,
                        branch_id     INTEGER,
                        notify_new    BOOLEAN NOT NULL DEFAULT 1,
                        notify_cancel BOOLEAN NOT NULL DEFAULT 1,
                        notify_noshow BOOLEAN NOT NULL DEFAULT 1,
                        quiet_start   TEXT,
                        quiet_end     TEXT
                    );
                    INSERT INTO admins (
                        user_id, role, is_active, added_by, created_at,
                        display_name, branch_id, notify_new, notify_cancel, notify_noshow,
                        quiet_start, quiet_end
                    )
                    SELECT
                        user_id, role, is_active, added_by, created_at,
                        display_name, branch_id, notify_new, notify_cancel, notify_noshow,
                        quiet_start, quiet_end
                    FROM _admins_legacy;
                    DROP TABLE _admins_legacy;
                    CREATE INDEX IF NOT EXISTS idx_admins_active ON admins (is_active);
                    """
                )
                await self.connection.commit()
            logger.info("Миграция: таблица admins обновлена с поддержкой роли viewer")

        for ddl in POST_MIGRATION_INDEXES:
            await self._execute(ddl)

    async def _seed_defaults(self) -> None:
        """Заполнить базовую точку, расписание ПН–ВС, тарифы, доп. услуги и настройки."""
        branch_row = await self._fetch_one("SELECT COUNT(*) AS n FROM branches")
        if branch_row is None or int(branch_row["n"]) == 0:
            await self._execute(
                """
                INSERT INTO branches (id, name, city, address, hours, phone, is_active, created_at)
                VALUES (1, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    DEFAULT_CLUB_NAME,
                    DEFAULT_CLUB_CITY,
                    DEFAULT_CLUB_ADDRESS,
                    DEFAULT_CLUB_HOURS,
                    DEFAULT_ADMIN_PHONE,
                    self.now(),
                ),
            )

        for weekday in range(7):
            await self._execute(
                """
                INSERT OR IGNORE INTO schedule_days (weekday, open_time, close_time, is_closed)
                VALUES (?, '00:00', '24:00', 0)
                """,
                (weekday,),
            )

        for item in _DEFAULT_TARIFFS:
            await self._execute(
                """
                INSERT OR IGNORE INTO tariffs_catalog (
                    key, title, emoji, pcs,
                    hour1, hour1_weekend, hour3, hour3_weekend, hour5, hour5_weekend,
                    morning, day, night, night_weekend, description, sort_order, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                item,
            )

        for extra in _DEFAULT_EXTRAS:
            await self._execute(
                """
                INSERT OR IGNORE INTO tariff_extras (key, title, emoji, price, is_active)
                VALUES (?, ?, ?, ?, ?)
                """,
                extra,
            )

        now_ts = self.now()
        for key, value in _DEFAULT_CLUB_SETTINGS.items():
            await self._execute(
                """
                INSERT OR IGNORE INTO club_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                (key, value, now_ts),
            )

    async def _fetch_one(self, sql: str, params: tuple[object, ...] = ()) -> Row | None:
        async with self._lock, self.connection.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def _fetch_all(self, sql: str, params: tuple[object, ...] = ()) -> list[Row]:
        async with self._lock, self.connection.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    async def _execute(self, sql: str, params: tuple[object, ...] = ()) -> int:
        async with self._lock:
            cursor = await self.connection.execute(sql, params)
            try:
                await self.connection.commit()
                return cursor.rowcount
            finally:
                await cursor.close()

    # ------------------------------------------------------------------ #
    # users
    # ------------------------------------------------------------------ #
    async def upsert_user(
        self,
        user_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ) -> User:
        await self._execute(
            """
            INSERT INTO users (user_id, username, first_name, consent_given, created_at)
            VALUES (?, ?, ?, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = COALESCE(excluded.username, users.username),
                first_name = COALESCE(excluded.first_name, users.first_name)
            """,
            (user_id, username, first_name, self.now()),
        )
        user = await self.get_user(user_id)
        if user is None:
            raise RuntimeError(
                f"Не удалось прочитать пользователя {user_id} после записи"
            )
        return user

    async def get_user(self, user_id: int) -> User | None:
        row = await self._fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return User.from_row(row) if row is not None else None

    async def find_user_by_username(self, username: str) -> User | None:
        clean = username.lstrip("@").strip().lower()
        if not clean:
            return None
        row = await self._fetch_one(
            "SELECT * FROM users WHERE LOWER(username) = ? LIMIT 1",
            (clean,),
        )
        return User.from_row(row) if row is not None else None

    async def has_consent(self, user_id: int) -> bool:
        row = await self._fetch_one(
            "SELECT consent_given FROM users WHERE user_id = ?",
            (user_id,),
        )
        return bool(row is not None and row["consent_given"])

    async def set_consent(self, user_id: int, granted: bool = True) -> None:
        await self.upsert_user(user_id)
        await self._execute(
            "UPDATE users SET consent_given = ? WHERE user_id = ?",
            (1 if granted else 0, user_id),
        )
        logger.info("Пользователь %s: consent_given=%s", user_id, granted)

    async def revoke_consent(self, user_id: int) -> None:
        await self.set_consent(user_id, granted=False)

    async def delete_user(self, user_id: int) -> bool:
        affected = await self._execute(
            "DELETE FROM users WHERE user_id = ?", (user_id,)
        )
        logger.info("Удаление пользователя %s: затронуто строк %s", user_id, affected)
        return affected > 0

    async def count_users(self) -> tuple[int, int]:
        row = await self._fetch_one(
            "SELECT COUNT(*) AS total, COALESCE(SUM(consent_given), 0) AS consented "
            "FROM users WHERE user_id > 0"
        )
        if row is None:
            return 0, 0
        return int(row["total"]), int(row["consented"])

    # ------------------------------------------------------------------ #
    # bookings
    # ------------------------------------------------------------------ #
    async def add_booking(
        self,
        user_id: int,
        tariff: str,
        time_slot: str,
        client_name: str,
        receipt_file_id: str | None,
        status: str = BookingStatus.PENDING,
        *,
        branch_id: int | None = 1,
        booking_date: str | None = None,
        slot_start: str | None = None,
        slot_end: str | None = None,
        guests_count: int = 1,
        seat_label: str | None = None,
        guest_comment: str | None = None,
        guest_contact: str | None = None,
        extras: Sequence[str] = (),
        quoted_price: int | None = None,
        receipt_kind: str = "photo",
        created_by_admin: bool = False,
        decided_by: int | None = None,
    ) -> Booking:
        """Сохранить заявку на бронирование и вернуть её вместе с присвоенным ``id``."""
        if status not in BookingStatus.ALL:
            raise ValueError(f"Недопустимый статус брони: {status!r}")

        now_ts = self.now()
        effective_date = booking_date or self.today_iso()
        decided_at = (
            now_ts
            if decided_by is not None and status != BookingStatus.PENDING
            else None
        )
        extras_payload = json.dumps(list(extras), ensure_ascii=False)

        cursor_id: int | None = None
        async with self._lock:
            cursor = await self.connection.execute(
                """
                INSERT INTO bookings (
                    user_id, tariff, time_slot, client_name, receipt_file_id, status, created_at,
                    decided_by, decided_at, branch_id, booking_date, slot_start, slot_end,
                    guests_count, seat_label, guest_comment, guest_contact, extras_json,
                    quoted_price, receipt_kind, created_by_admin
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    tariff,
                    time_slot,
                    client_name,
                    receipt_file_id,
                    status,
                    now_ts,
                    decided_by,
                    decided_at,
                    branch_id if branch_id is not None else 1,
                    effective_date,
                    slot_start,
                    slot_end,
                    max(1, int(guests_count)),
                    seat_label,
                    guest_comment,
                    guest_contact,
                    extras_payload,
                    quoted_price,
                    receipt_kind or "photo",
                    1 if created_by_admin else 0,
                ),
            )
            try:
                cursor_id = cursor.lastrowid
                await self.connection.commit()
            finally:
                await cursor.close()

        if not cursor_id:
            raise RuntimeError("SQLite не вернул id созданной брони")

        booking = await self.get_booking(cursor_id)
        if booking is None:
            raise RuntimeError(f"Созданная бронь {cursor_id} не читается из базы")
        logger.info("Новая бронь №%s: user=%s tariff=%s", booking.id, user_id, tariff)
        return booking

    async def create_manual_booking(
        self,
        *,
        admin_id: int,
        tariff: str,
        time_slot: str,
        client_name: str,
        branch_id: int = 1,
        booking_date: str | None = None,
        slot_start: str | None = None,
        slot_end: str | None = None,
        guests_count: int = 1,
        seat_label: str | None = None,
        guest_contact: str | None = None,
        guest_comment: str | None = None,
        quoted_price: int | None = None,
        target_user_id: int | None = None,
    ) -> Booking:
        """Создать подтверждённую бронь от лица администратора (звонок / ВК / стойка)."""
        uid = target_user_id
        if uid is None and guest_contact and guest_contact.strip().startswith("@"):
            matched = await self.find_user_by_username(guest_contact.strip())
            if matched is not None:
                uid = matched.user_id

        if uid is None:
            # Отрицательный user_id для офлайн-гостей (не смешивается с реальными Telegram ID > 0)
            row = await self._fetch_one(
                "SELECT COALESCE(MIN(user_id), 0) AS min_id FROM users"
            )
            min_id = int(row["min_id"]) if row is not None else 0
            uid = min(min_id, 0) - 1
            uname = (
                guest_contact.strip().lstrip("@")
                if guest_contact and guest_contact.strip().startswith("@")
                else None
            )
            await self._execute(
                """
                INSERT INTO users (user_id, username, first_name, consent_given, created_at)
                VALUES (?, ?, ?, 1, ?)
                """,
                (uid, uname, client_name, self.now()),
            )

        return await self.add_booking(
            user_id=uid,
            tariff=tariff,
            time_slot=time_slot,
            client_name=client_name,
            receipt_file_id=None,
            status=BookingStatus.APPROVED,
            branch_id=branch_id,
            booking_date=booking_date or self.today_iso(),
            slot_start=slot_start,
            slot_end=slot_end,
            guests_count=guests_count,
            seat_label=seat_label,
            guest_comment=guest_comment,
            guest_contact=guest_contact,
            quoted_price=quoted_price,
            created_by_admin=True,
            decided_by=admin_id,
        )

    async def get_booking(self, booking_id: int) -> Booking | None:
        row = await self._fetch_one(
            "SELECT * FROM bookings WHERE id = ?", (booking_id,)
        )
        return Booking.from_row(row) if row is not None else None

    async def set_status_if_pending(
        self,
        booking_id: int,
        status: str,
        decided_by: int | None = None,
        *,
        reject_reason: str | None = None,
        reject_comment: str | None = None,
    ) -> bool:
        """Перевести заявку из ``pending`` в ``approved`` / ``rejected``."""
        if status not in (BookingStatus.APPROVED, BookingStatus.REJECTED):
            raise ValueError(
                f"Статус после решения должен быть approved/rejected, дано: {status!r}"
            )

        reason = (
            (reject_reason or RejectReason.NO_SEATS)
            if status == BookingStatus.REJECTED
            else None
        )
        affected = await self._execute(
            """
            UPDATE bookings
               SET status = ?,
                   decided_by = ?,
                   decided_at = ?,
                   reject_reason = COALESCE(?, reject_reason),
                   reject_comment = COALESCE(?, reject_comment)
             WHERE id = ? AND status = 'pending'
            """,
            (status, decided_by, self.now(), reason, reject_comment, booking_id),
        )
        return affected == 1

    async def cancel_approved_booking(
        self,
        booking_id: int,
        *,
        decided_by: int | None,
        reason: str = RejectReason.ADMIN_CANCELED,
        comment: str | None = None,
    ) -> bool:
        """Отменить уже подтверждённую бронь с сохранением причины и флага ``was_approved``."""
        affected = await self._execute(
            """
            UPDATE bookings
               SET status = 'rejected',
                   was_approved = 1,
                   no_show = ?,
                   decided_by = ?,
                   decided_at = ?,
                   reject_reason = ?,
                   reject_comment = ?
             WHERE id = ? AND status = 'approved'
            """,
            (
                1 if reason == RejectReason.NO_SHOW else 0,
                decided_by,
                self.now(),
                reason,
                comment,
                booking_id,
            ),
        )
        return affected == 1

    async def mark_booking_noshow(
        self, booking_id: int, *, decided_by: int | None
    ) -> bool:
        """Отметить неявку гостя по подтверждённой брони."""
        return await self.cancel_approved_booking(
            booking_id,
            decided_by=decided_by,
            reason=RejectReason.NO_SHOW,
            comment="гость не пришёл к назначенному времени",
        )

    async def client_cancel_booking(
        self, booking_id: int, user_id: int
    ) -> Booking | None:
        """Отмена брони самим гостем из раздела «Мои брони»."""
        current = await self.get_booking(booking_id)
        if current is None or current.user_id != user_id:
            return None
        if current.status not in (BookingStatus.PENDING, BookingStatus.APPROVED):
            return None
        was_app = 1 if current.status == BookingStatus.APPROVED else 0
        affected = await self._execute(
            """
            UPDATE bookings
               SET status = 'rejected',
                   was_approved = ?,
                   decided_at = ?,
                   reject_reason = ?
             WHERE id = ? AND user_id = ? AND status IN ('pending', 'approved')
            """,
            (was_app, self.now(), RejectReason.GUEST_CANCELED, booking_id, user_id),
        )
        if affected != 1:
            return None
        return await self.get_booking(booking_id)

    async def update_booking_details(
        self,
        booking_id: int,
        *,
        tariff: Any = _UNSET,
        time_slot: Any = _UNSET,
        booking_date: Any = _UNSET,
        slot_start: Any = _UNSET,
        slot_end: Any = _UNSET,
        guests_count: Any = _UNSET,
        seat_label: Any = _UNSET,
        quoted_price: Any = _UNSET,
        branch_id: Any = _UNSET,
    ) -> Booking | None:
        """Обновить параметры брони (время, количество гостей, зал / ПК, точку)."""
        updates: list[str] = []
        params: list[object] = []
        mapping = (
            ("tariff", tariff),
            ("time_slot", time_slot),
            ("booking_date", booking_date),
            ("slot_start", slot_start),
            ("slot_end", slot_end),
            ("guests_count", guests_count),
            ("seat_label", seat_label),
            ("quoted_price", quoted_price),
            ("branch_id", branch_id),
        )
        for col, val in mapping:
            if val is not _UNSET:
                updates.append(f"{col} = ?")
                params.append(val)
        if not updates:
            return await self.get_booking(booking_id)
        params.append(booking_id)
        await self._execute(
            f"UPDATE bookings SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
        return await self.get_booking(booking_id)

    # ------------------------------------------------------------------ #
    # branches — точки сети
    # ------------------------------------------------------------------ #
    async def list_branches(self, *, active_only: bool = False) -> list[Branch]:
        sql = (
            "SELECT * FROM branches WHERE is_active = 1 ORDER BY id"
            if active_only
            else "SELECT * FROM branches ORDER BY id"
        )
        rows = await self._fetch_all(sql)
        return [Branch.from_row(row) for row in rows]

    async def get_branch(self, branch_id: int | None) -> Branch | None:
        if branch_id is None:
            branches = await self.list_branches(active_only=True)
            return branches[0] if branches else None
        row = await self._fetch_one("SELECT * FROM branches WHERE id = ?", (branch_id,))
        return Branch.from_row(row) if row is not None else None

    async def add_branch(
        self,
        name: str,
        *,
        city: str = "",
        address: str = "",
        hours: str = DEFAULT_CLUB_HOURS,
        phone: str = "",
    ) -> Branch:
        async with self._lock:
            cursor = await self.connection.execute(
                """
                INSERT INTO branches (name, city, address, hours, phone, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    name.strip(),
                    city.strip(),
                    address.strip(),
                    hours.strip(),
                    phone.strip(),
                    self.now(),
                ),
            )
            try:
                bid = cursor.lastrowid
                await self.connection.commit()
            finally:
                await cursor.close()
        branch = await self.get_branch(int(bid or 1))
        if branch is None:
            raise RuntimeError("Не удалось прочитать созданную точку")
        return branch

    async def update_branch(
        self,
        branch_id: int,
        *,
        name: str | None = None,
        city: str | None = None,
        address: str | None = None,
        hours: str | None = None,
        phone: str | None = None,
    ) -> Branch | None:
        cur = await self.get_branch(branch_id)
        if cur is None:
            return None
        await self._execute(
            """
            UPDATE branches
               SET name = ?, city = ?, address = ?, hours = ?, phone = ?
             WHERE id = ?
            """,
            (
                name if name is not None else cur.name,
                city if city is not None else cur.city,
                address if address is not None else cur.address,
                hours if hours is not None else cur.hours,
                phone if phone is not None else cur.phone,
                branch_id,
            ),
        )
        return await self.get_branch(branch_id)

    async def count_active_branches(self) -> int:
        return len(await self.list_branches(active_only=True))

    async def set_branch_active(self, branch_id: int, active: bool) -> bool:
        if not active:
            active_list = await self.list_branches(active_only=True)
            if len(active_list) <= 1 and any(b.id == branch_id for b in active_list):
                return False
        affected = await self._execute(
            "UPDATE branches SET is_active = ? WHERE id = ?",
            (1 if active else 0, branch_id),
        )
        return affected == 1

    # ------------------------------------------------------------------ #
    # admins — роли, привязка к точкам, приглашения, уведомления
    # ------------------------------------------------------------------ #
    async def add_admin(
        self,
        user_id: int,
        role: str = AdminRole.OPERATOR,
        added_by: int | None = None,
        *,
        display_name: str | None = None,
        branch_id: int | None = None,
    ) -> Admin:
        norm_role = AdminRole.normalize(role)
        if norm_role not in AdminRole.ALL:
            raise ValueError(f"Недопустимая роль администратора: {role!r}")

        await self._execute(
            """
            INSERT INTO admins (user_id, role, is_active, added_by, created_at, display_name, branch_id)
            VALUES (?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                role         = excluded.role,
                is_active    = 1,
                added_by     = COALESCE(excluded.added_by, admins.added_by),
                display_name = COALESCE(excluded.display_name, admins.display_name),
                branch_id    = COALESCE(excluded.branch_id, admins.branch_id)
            """,
            (user_id, norm_role, added_by, self.now(), display_name, branch_id),
        )
        admin = await self.get_admin(user_id)
        if admin is None:
            raise RuntimeError(
                f"Не удалось прочитать администратора {user_id} после записи"
            )
        logger.info("Администратор %s сохранён (роль %s)", user_id, norm_role)
        return admin

    async def get_admin(self, user_id: int) -> Admin | None:
        row = await self._fetch_one(
            "SELECT * FROM admins WHERE user_id = ?", (user_id,)
        )
        return Admin.from_row(row) if row is not None else None

    async def is_active_admin(self, user_id: int) -> bool:
        row = await self._fetch_one(
            "SELECT is_active FROM admins WHERE user_id = ?",
            (user_id,),
        )
        return bool(row is not None and row["is_active"])

    async def list_admins(
        self, *, active_only: bool = False, branch_id: int | None = None
    ) -> list[Admin]:
        clauses: list[str] = []
        params: list[object] = []
        if active_only:
            clauses.append("is_active = 1")
        if branch_id is not None:
            clauses.append("branch_id = ?")
            params.append(branch_id)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = await self._fetch_all(
            f"SELECT * FROM admins {where}"
            "ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'operator' THEN 1 ELSE 2 END, "
            "created_at, user_id",
            tuple(params),
        )
        return [Admin.from_row(row) for row in rows]

    async def active_admin_ids(self) -> tuple[int, ...]:
        rows = await self._fetch_all(
            "SELECT user_id FROM admins WHERE is_active = 1 "
            "ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'operator' THEN 1 ELSE 2 END, "
            "created_at, user_id"
        )
        return tuple(int(row["user_id"]) for row in rows)

    async def set_admin_active(self, user_id: int, active: bool) -> bool:
        affected = await self._execute(
            "UPDATE admins SET is_active = ? WHERE user_id = ?",
            (1 if active else 0, user_id),
        )
        return affected == 1

    async def set_admin_role(self, user_id: int, role: str) -> bool:
        norm_role = AdminRole.normalize(role)
        if norm_role not in AdminRole.ALL:
            raise ValueError(f"Недопустимая роль администратора: {role!r}")
        affected = await self._execute(
            "UPDATE admins SET role = ? WHERE user_id = ?",
            (norm_role, user_id),
        )
        return affected == 1

    async def set_admin_branch(self, user_id: int, branch_id: int | None) -> bool:
        affected = await self._execute(
            "UPDATE admins SET branch_id = ? WHERE user_id = ?",
            (branch_id, user_id),
        )
        return affected == 1

    async def set_admin_display_name(
        self, user_id: int, display_name: str | None
    ) -> bool:
        affected = await self._execute(
            "UPDATE admins SET display_name = ? WHERE user_id = ?",
            ((display_name or "").strip() or None, user_id),
        )
        return affected == 1

    async def transfer_branch_admin(
        self,
        branch_id: int,
        *,
        from_admin_id: int,
        to_admin_id: int,
        deactivate_from: bool = False,
    ) -> bool:
        """Передать закреплённую точку от одного администратора другому (смена персонала)."""
        await self._execute(
            "UPDATE admins SET branch_id = ? WHERE user_id = ?",
            (branch_id, to_admin_id),
        )
        if from_admin_id != to_admin_id:
            await self._execute(
                "UPDATE admins SET branch_id = NULL WHERE user_id = ? AND branch_id = ?",
                (from_admin_id, branch_id),
            )
            if deactivate_from:
                await self.set_admin_active(from_admin_id, False)
        return True

    async def update_admin_notifications(
        self,
        user_id: int,
        *,
        notify_new: bool | None = None,
        notify_cancel: bool | None = None,
        notify_noshow: bool | None = None,
        quiet_start: Any = _UNSET,
        quiet_end: Any = _UNSET,
    ) -> Admin | None:
        current = await self.get_admin(user_id)
        if current is None:
            return None
        q_start = current.quiet_start if quiet_start is _UNSET else quiet_start
        q_end = current.quiet_end if quiet_end is _UNSET else quiet_end
        await self._execute(
            """
            UPDATE admins
               SET notify_new = ?,
                   notify_cancel = ?,
                   notify_noshow = ?,
                   quiet_start = ?,
                   quiet_end = ?
             WHERE user_id = ?
            """,
            (
                1 if (current.notify_new if notify_new is None else notify_new) else 0,
                1
                if (current.notify_cancel if notify_cancel is None else notify_cancel)
                else 0,
                1
                if (current.notify_noshow if notify_noshow is None else notify_noshow)
                else 0,
                q_start,
                q_end,
                user_id,
            ),
        )
        return await self.get_admin(user_id)

    async def create_admin_invite(
        self,
        *,
        name: str = "",
        role: str = AdminRole.OPERATOR,
        branch_id: int | None = None,
        created_by: int | None = None,
    ) -> AdminInvite:
        token = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
        norm_role = AdminRole.normalize(role)
        await self._execute(
            """
            INSERT INTO admin_invites (token, name, role, branch_id, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (token, name.strip(), norm_role, branch_id, created_by, self.now()),
        )
        row = await self._fetch_one(
            "SELECT * FROM admin_invites WHERE token = ?", (token,)
        )
        if row is None:
            raise RuntimeError("Не удалось создать приглашение администратора")
        return AdminInvite.from_row(row)

    async def consume_admin_invite(self, token: str, user_id: int) -> Admin | None:
        row = await self._fetch_one(
            "SELECT * FROM admin_invites WHERE token = ? AND used_by IS NULL",
            (token,),
        )
        if row is None:
            return None
        invite = AdminInvite.from_row(row)
        await self._execute(
            "UPDATE admin_invites SET used_by = ?, used_at = ? WHERE token = ?",
            (user_id, self.now(), token),
        )
        return await self.add_admin(
            user_id,
            role=invite.role,
            added_by=invite.created_by,
            display_name=invite.name or None,
            branch_id=invite.branch_id,
        )

    async def count_active_owners(self) -> int:
        row = await self._fetch_one(
            "SELECT COUNT(*) AS n FROM admins WHERE role = ? AND is_active = 1",
            (AdminRole.OWNER,),
        )
        return int(row["n"]) if row is not None else 0

    async def remove_admin(self, user_id: int) -> bool:
        affected = await self._execute(
            "DELETE FROM admins WHERE user_id = ?", (user_id,)
        )
        logger.info("Администратор %s удалён: %s", user_id, affected == 1)
        return affected == 1

    async def count_admins(self) -> tuple[int, int]:
        row = await self._fetch_one(
            "SELECT COUNT(*) AS total, COALESCE(SUM(is_active), 0) AS active FROM admins"
        )
        if row is None:
            return 0, 0
        return int(row["total"]), int(row["active"])

    async def sync_env_admins(self, admin_ids: tuple[int, ...] | list[int]) -> None:
        for index, admin_id in enumerate(admin_ids):
            existing = await self.get_admin(admin_id)
            if index == 0:
                await self.add_admin(admin_id, role=AdminRole.OWNER, added_by=None)
            elif existing is None:
                await self.add_admin(admin_id, role=AdminRole.OPERATOR, added_by=None)
            else:
                await self.set_admin_active(admin_id, True)

    # ------------------------------------------------------------------ #
    # schedule_days / schedule_holidays / external_busy_slots
    # ------------------------------------------------------------------ #
    async def list_schedule_days(self) -> list[ScheduleDay]:
        rows = await self._fetch_all("SELECT * FROM schedule_days ORDER BY weekday")
        return [ScheduleDay.from_row(row) for row in rows]

    async def get_schedule_day(self, weekday: int) -> ScheduleDay:
        row = await self._fetch_one(
            "SELECT * FROM schedule_days WHERE weekday = ?",
            (int(weekday) % 7,),
        )
        if row is None:
            return ScheduleDay(weekday=int(weekday) % 7)
        return ScheduleDay.from_row(row)

    async def set_schedule_day(
        self,
        weekday: int,
        open_time: str,
        close_time: str,
        *,
        is_closed: bool = False,
    ) -> ScheduleDay:
        wd = int(weekday) % 7
        await self._execute(
            """
            INSERT INTO schedule_days (weekday, open_time, close_time, is_closed)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(weekday) DO UPDATE SET
                open_time  = excluded.open_time,
                close_time = excluded.close_time,
                is_closed  = excluded.is_closed
            """,
            (wd, open_time, close_time, 1 if is_closed else 0),
        )
        return await self.get_schedule_day(wd)

    async def list_holidays(self) -> list[ScheduleHoliday]:
        rows = await self._fetch_all(
            "SELECT * FROM schedule_holidays ORDER BY holiday_date"
        )
        return [ScheduleHoliday.from_row(row) for row in rows]

    async def get_holiday_for_date(self, iso_date: str) -> ScheduleHoliday | None:
        mm_dd = iso_date[5:] if len(iso_date) >= 10 else iso_date
        row = await self._fetch_one(
            "SELECT * FROM schedule_holidays WHERE holiday_date IN (?, ?) "
            "ORDER BY LENGTH(holiday_date) DESC LIMIT 1",
            (iso_date, mm_dd),
        )
        return ScheduleHoliday.from_row(row) if row is not None else None

    async def upsert_holiday(
        self,
        holiday_date: str,
        *,
        title: str = "Праздничный день",
        open_time: str = "12:00",
        close_time: str = "24:00",
        is_closed: bool = False,
    ) -> ScheduleHoliday:
        await self._execute(
            """
            INSERT INTO schedule_holidays (holiday_date, title, open_time, close_time, is_closed)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(holiday_date) DO UPDATE SET
                title      = excluded.title,
                open_time  = excluded.open_time,
                close_time = excluded.close_time,
                is_closed  = excluded.is_closed
            """,
            (
                holiday_date.strip(),
                title.strip(),
                open_time,
                close_time,
                1 if is_closed else 0,
            ),
        )
        row = await self._fetch_one(
            "SELECT * FROM schedule_holidays WHERE holiday_date = ?",
            (holiday_date.strip(),),
        )
        if row is None:
            raise RuntimeError("Не удалось сохранить праздничную дату")
        return ScheduleHoliday.from_row(row)

    async def delete_holiday(self, holiday_date: str) -> bool:
        affected = await self._execute(
            "DELETE FROM schedule_holidays WHERE holiday_date = ?",
            (holiday_date.strip(),),
        )
        return affected == 1

    async def list_external_busy_slots(
        self,
        *,
        slot_date: str | None = None,
        branch_id: int | None = None,
    ) -> list[ExternalBusySlot]:
        clauses: list[str] = []
        params: list[object] = []
        if slot_date is not None:
            clauses.append("slot_date = ?")
            params.append(slot_date)
        if branch_id is not None:
            clauses.append("branch_id = ?")
            params.append(branch_id)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = await self._fetch_all(
            f"SELECT * FROM external_busy_slots {where}ORDER BY slot_date, start_time, id",
            tuple(params),
        )
        return [ExternalBusySlot.from_row(row) for row in rows]

    async def add_external_busy_slot(
        self,
        slot_date: str,
        start_time: str,
        end_time: str,
        *,
        branch_id: int = 1,
        seats_taken: int = 16,
        source: str = "izi",
        note: str = "",
    ) -> ExternalBusySlot:
        async with self._lock:
            cursor = await self.connection.execute(
                """
                INSERT INTO external_busy_slots
                    (branch_id, slot_date, start_time, end_time, seats_taken, source, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (branch_id, slot_date, start_time, end_time, seats_taken, source, note),
            )
            try:
                sid = cursor.lastrowid
                await self.connection.commit()
            finally:
                await cursor.close()
        row = await self._fetch_one(
            "SELECT * FROM external_busy_slots WHERE id = ?", (sid,)
        )
        if row is None:
            raise RuntimeError("Не удалось записать занятый слот")
        return ExternalBusySlot.from_row(row)

    async def delete_external_busy_slot(self, slot_id: int) -> bool:
        affected = await self._execute(
            "DELETE FROM external_busy_slots WHERE id = ?", (slot_id,)
        )
        return affected == 1

    # ------------------------------------------------------------------ #
    # tariffs_catalog & tariff_extras
    # ------------------------------------------------------------------ #
    async def list_tariffs(self, *, active_only: bool = True) -> list[TariffRecord]:
        sql = (
            "SELECT * FROM tariffs_catalog WHERE is_active = 1 ORDER BY sort_order, key"
            if active_only
            else "SELECT * FROM tariffs_catalog ORDER BY sort_order, key"
        )
        rows = await self._fetch_all(sql)
        return [TariffRecord.from_row(row) for row in rows]

    async def get_tariff_record(self, key: str) -> TariffRecord | None:
        row = await self._fetch_one(
            "SELECT * FROM tariffs_catalog WHERE LOWER(key) = LOWER(?) OR LOWER(title) = LOWER(?)",
            (key.strip(), key.strip()),
        )
        return TariffRecord.from_row(row) if row is not None else None

    async def update_tariff_prices(
        self,
        key: str,
        *,
        hour1: int | None = None,
        hour1_weekend: int | None = None,
        hour3: int | None = None,
        hour3_weekend: int | None = None,
        hour5: int | None = None,
        hour5_weekend: int | None = None,
        morning: int | None = None,
        day: int | None = None,
        night: int | None = None,
        night_weekend: int | None = None,
        pcs: int | None = None,
    ) -> TariffRecord | None:
        cur = await self.get_tariff_record(key)
        if cur is None:
            return None
        await self._execute(
            """
            UPDATE tariffs_catalog
               SET hour1 = ?, hour1_weekend = ?,
                   hour3 = ?, hour3_weekend = ?,
                   hour5 = ?, hour5_weekend = ?,
                   morning = ?, day = ?,
                   night = ?, night_weekend = ?,
                   pcs = ?
             WHERE key = ?
            """,
            (
                hour1 if hour1 is not None else cur.hour1,
                hour1_weekend if hour1_weekend is not None else cur.hour1_weekend,
                hour3 if hour3 is not None else cur.hour3,
                hour3_weekend if hour3_weekend is not None else cur.hour3_weekend,
                hour5 if hour5 is not None else cur.hour5,
                hour5_weekend if hour5_weekend is not None else cur.hour5_weekend,
                morning if morning is not None else cur.morning,
                day if day is not None else cur.day,
                night if night is not None else cur.night,
                night_weekend if night_weekend is not None else cur.night_weekend,
                pcs if pcs is not None else cur.pcs,
                cur.key,
            ),
        )
        return await self.get_tariff_record(cur.key)

    async def list_tariff_extras(
        self, *, active_only: bool = False
    ) -> list[TariffExtra]:
        sql = (
            "SELECT * FROM tariff_extras WHERE is_active = 1 ORDER BY rowid"
            if active_only
            else "SELECT * FROM tariff_extras ORDER BY rowid"
        )
        rows = await self._fetch_all(sql)
        return [TariffExtra.from_row(row) for row in rows]

    async def get_tariff_extra(self, key: str) -> TariffExtra | None:
        row = await self._fetch_one("SELECT * FROM tariff_extras WHERE key = ?", (key,))
        return TariffExtra.from_row(row) if row is not None else None

    async def upsert_tariff_extra(
        self,
        key: str,
        title: str,
        *,
        emoji: str = "✨",
        price: int = 0,
        is_active: bool = True,
    ) -> TariffExtra:
        await self._execute(
            """
            INSERT INTO tariff_extras (key, title, emoji, price, is_active)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                title     = excluded.title,
                emoji     = excluded.emoji,
                price     = excluded.price,
                is_active = excluded.is_active
            """,
            (
                key.strip(),
                title.strip(),
                emoji.strip() or "✨",
                max(0, int(price)),
                1 if is_active else 0,
            ),
        )
        extra = await self.get_tariff_extra(key.strip())
        if extra is None:
            raise RuntimeError("Не удалось сохранить доп. услугу")
        return extra

    async def toggle_tariff_extra(self, key: str) -> TariffExtra | None:
        cur = await self.get_tariff_extra(key)
        if cur is None:
            return None
        await self._execute(
            "UPDATE tariff_extras SET is_active = ? WHERE key = ?",
            (0 if cur.is_active else 1, key),
        )
        return await self.get_tariff_extra(key)

    # ------------------------------------------------------------------ #
    # club_settings
    # ------------------------------------------------------------------ #
    async def get_settings(self) -> dict[str, str]:
        rows = await self._fetch_all("SELECT key, value FROM club_settings")
        out = dict(_DEFAULT_CLUB_SETTINGS)
        for row in rows:
            out[str(row["key"])] = str(row["value"])
        return out

    async def get_setting(self, key: str, default: str = "") -> str:
        row = await self._fetch_one(
            "SELECT value FROM club_settings WHERE key = ?", (key,)
        )
        if row is not None and row["value"] is not None:
            return str(row["value"])
        return _DEFAULT_CLUB_SETTINGS.get(key, default)

    async def set_setting(self, key: str, value: str) -> None:
        await self._execute(
            """
            INSERT INTO club_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value      = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, self.now()),
        )

    async def set_settings(self, values: dict[str, str]) -> None:
        for k, v in values.items():
            await self.set_setting(k, v)

    # ------------------------------------------------------------------ #
    # booking_notifications & выборки очереди
    # ------------------------------------------------------------------ #
    async def add_notification(
        self, booking_id: int, admin_id: int, message_id: int
    ) -> None:
        await self._execute(
            """
            INSERT OR IGNORE INTO booking_notifications
                (booking_id, admin_id, message_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (booking_id, admin_id, message_id, self.now()),
        )

    async def list_notifications(self, booking_id: int) -> list[tuple[int, int]]:
        rows = await self._fetch_all(
            "SELECT admin_id, message_id FROM booking_notifications WHERE booking_id = ? ORDER BY id",
            (booking_id,),
        )
        return [(int(row["admin_id"]), int(row["message_id"])) for row in rows]

    async def list_bookings(
        self,
        status: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
        oldest_first: bool = False,
        branch_id: int | None = None,
        for_date: str | None = None,
    ) -> list[Booking]:
        order = "ASC" if oldest_first else "DESC"
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if branch_id is not None:
            clauses.append("COALESCE(branch_id, 1) = ?")
            params.append(branch_id)
        if for_date is not None:
            clauses.append("COALESCE(booking_date, SUBSTR(created_at, 1, 10)) = ?")
            params.append(for_date)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.extend((limit, offset))
        rows = await self._fetch_all(
            f"SELECT * FROM bookings {where}ORDER BY id {order} LIMIT ? OFFSET ?",
            tuple(params),
        )
        return [Booking.from_row(row) for row in rows]

    async def list_bookings_with_users(
        self,
        status: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
        oldest_first: bool = False,
        branch_id: int | None = None,
        for_date: str | None = None,
    ) -> list[tuple[Booking, User | None]]:
        order = "ASC" if oldest_first else "DESC"
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("b.status = ?")
            params.append(status)
        if branch_id is not None:
            clauses.append("COALESCE(b.branch_id, 1) = ?")
            params.append(branch_id)
        if for_date is not None:
            clauses.append("COALESCE(b.booking_date, SUBSTR(b.created_at, 1, 10)) = ?")
            params.append(for_date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend((limit, offset))
        rows = await self._fetch_all(
            f"""
            SELECT b.*, u.user_id AS u_id, u.username AS u_username, u.first_name AS u_first_name
              FROM bookings b
              LEFT JOIN users u ON u.user_id = b.user_id
              {where}
             ORDER BY b.id {order}
             LIMIT ? OFFSET ?
            """,
            tuple(params),
        )

        result: list[tuple[Booking, User | None]] = []
        for row in rows:
            user = (
                User(
                    user_id=int(row["u_id"]),
                    username=row["u_username"],
                    first_name=row["u_first_name"],
                    consent_given=True,
                    created_at="",
                )
                if row["u_id"] is not None and int(row["u_id"]) > 0
                else None
            )
            result.append((Booking.from_row(row), user))
        return result

    async def list_user_bookings(
        self, user_id: int, *, limit: int = 10
    ) -> list[Booking]:
        rows = await self._fetch_all(
            "SELECT * FROM bookings WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return [Booking.from_row(row) for row in rows]

    async def count_bookings(
        self,
        status: str | None = None,
        *,
        branch_id: int | None = None,
        for_date: str | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if branch_id is not None:
            clauses.append("COALESCE(branch_id, 1) = ?")
            params.append(branch_id)
        if for_date is not None:
            clauses.append("COALESCE(booking_date, SUBSTR(created_at, 1, 10)) = ?")
            params.append(for_date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = await self._fetch_one(
            f"SELECT COUNT(*) AS n FROM bookings {where}", tuple(params)
        )
        return int(row["n"]) if row is not None else 0

    async def list_tables(self) -> list[str]:
        rows = await self._fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        return [str(row["name"]) for row in rows]

    async def get_stats(self, *, branch_id: int | None = None) -> BookingStats:
        """Сводка для админ-панели (общая или по закреплённой точке администратора)."""
        branch_where = (
            "WHERE COALESCE(branch_id, 1) = ?" if branch_id is not None else ""
        )
        branch_params: tuple[object, ...] = (
            (branch_id,) if branch_id is not None else ()
        )

        rows = await self._fetch_all(
            f"SELECT status, COUNT(*) AS n FROM bookings {branch_where} GROUP BY status",
            branch_params,
        )
        by_status: dict[str, int] = {str(row["status"]): int(row["n"]) for row in rows}
        users_total, users_consented = await self.count_users()
        admins_total, admins_active = await self.count_admins()

        today = self.today_iso()
        approved_today = await self.count_bookings(
            BookingStatus.APPROVED, branch_id=branch_id, for_date=today
        )
        rejected_today = await self.count_bookings(
            BookingStatus.REJECTED, branch_id=branch_id, for_date=today
        )

        extra_where = (
            "WHERE created_by_admin = 1 AND COALESCE(branch_id, 1) = ?"
            if branch_id is not None
            else "WHERE created_by_admin = 1"
        )
        manual_row = await self._fetch_one(
            f"SELECT COUNT(*) AS n FROM bookings {extra_where}",
            branch_params,
        )
        manual_count = int(manual_row["n"]) if manual_row is not None else 0

        noshow_where = (
            "WHERE no_show = 1 AND COALESCE(branch_id, 1) = ?"
            if branch_id is not None
            else "WHERE no_show = 1"
        )
        noshow_row = await self._fetch_one(
            f"SELECT COUNT(*) AS n FROM bookings {noshow_where}",
            branch_params,
        )
        noshow_count = int(noshow_row["n"]) if noshow_row is not None else 0

        branches_active = len(await self.list_branches(active_only=True))

        total = sum(by_status.values())
        return BookingStats(
            total=total,
            pending=by_status.get(BookingStatus.PENDING, 0),
            approved=by_status.get(BookingStatus.APPROVED, 0),
            rejected=by_status.get(BookingStatus.REJECTED, 0),
            users=users_total,
            consented=users_consented,
            admins=admins_total,
            admins_active=admins_active,
            manual=manual_count,
            approved_today=approved_today,
            rejected_today=rejected_today,
            no_shows=noshow_count,
            branches_active=max(1, branches_active),
        )


async def init_database(path: str | Path, timezone: ZoneInfo) -> Database:
    database = Database(path, timezone=timezone)
    try:
        await database.connect()
    except BaseException:
        await database.close()
        raise
    return database


__all__ = [
    "ADMINS_MIGRATIONS",
    "BOOKINGS_MIGRATIONS",
    "POST_MIGRATION_INDEXES",
    "Database",
    "init_database",
]
