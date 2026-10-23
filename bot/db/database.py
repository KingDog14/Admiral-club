"""Репозиторий SQLite поверх :mod:`aiosqlite`.

Особенности реализации:

* одно соединение на процесс, все операции сериализуются через
  :class:`asyncio.Lock` — SQLite не любит параллельной записи, а нагрузка
  клубного бота это позволяет;
* ``PRAGMA journal_mode=WAL`` — чтение не блокируется записью, база не «пухнет»
  от временных журналов;
* ``created_at`` пишется в ISO-8601 с часовым поясом клуба, поэтому в
  админ-панели время заявки отображается корректно для любого региона;
* схема создаётся автоматически из ``schema.sql`` — отдельный шаг «создать
  базу» не требуется (но доступен: ``python main.py --init-db``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from sqlite3 import Row
from zoneinfo import ZoneInfo

import aiosqlite

from bot.db.models import Admin, AdminRole, Booking, BookingStats, BookingStatus, User

logger = logging.getLogger(__name__)

SCHEMA_FILE: Path = Path(__file__).with_name("schema.sql")

#: Столбцы, которые добавляются в уже существующие базы данных (мягкая миграция).
BOOKINGS_MIGRATIONS: dict[str, str] = {
    "decided_by": "ALTER TABLE bookings ADD COLUMN decided_by INTEGER",
    "decided_at": "ALTER TABLE bookings ADD COLUMN decided_at DATETIME",
}

#: Индексы, создаваемые после миграции столбцов (в старой базе их ещё нет).
POST_MIGRATION_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_bookings_decided ON bookings (decided_by)",
)


class Database:
    """Асинхронный доступ к базе данных бота."""

    def __init__(self, path: str | Path, *, timezone: ZoneInfo) -> None:
        self._path = Path(path)
        self._timezone = timezone
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Подключение
    # ------------------------------------------------------------------ #
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
        """Открыть соединение, включить PRAGMA и применить схему."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self._path)
        self._connection.row_factory = aiosqlite.Row

        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA synchronous=NORMAL")
        await self._connection.execute("PRAGMA foreign_keys=ON")
        await self._connection.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        await self._connection.commit()
        await self._migrate()

        logger.info("База данных готова: %s", self._path)

    async def close(self) -> None:
        """Закрыть соединение (вызывается при остановке бота)."""
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
            logger.info("Соединение с базой данных закрыто")

    def now(self) -> str:
        """Текущее время клуба в ISO-8601 (пишется в ``created_at``)."""
        return datetime.now(self._timezone).isoformat(timespec="seconds")

    async def _table_columns(self, table: str) -> set[str]:
        """Набор столбцов таблицы — нужен для мягкой миграции старых баз."""
        if not table.isidentifier():  # защита от подстановки в PRAGMA
            raise ValueError(f"Некорректное имя таблицы: {table!r}")
        async with self._lock, self.connection.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
        return {str(row["name"]) for row in rows}

    async def _migrate(self) -> None:
        """Добавить недостающие столбцы в базу, созданную предыдущей версией бота.

        ``schema.sql`` использует ``CREATE TABLE IF NOT EXISTS``, поэтому в уже
        существующей базе новые столбцы сами не появятся — добавляем их через
        ``ALTER TABLE``. Операция идемпотентна и не трогает данные.
        """
        columns = await self._table_columns("bookings")
        for column, ddl in BOOKINGS_MIGRATIONS.items():
            if column not in columns:
                await self._execute(ddl)
                logger.info("Миграция: в таблицу bookings добавлен столбец %s", column)
        for ddl in POST_MIGRATION_INDEXES:
            await self._execute(ddl)

    async def _fetch_one(self, sql: str, params: tuple[object, ...] = ()) -> Row | None:
        async with self._lock, self.connection.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def _fetch_all(self, sql: str, params: tuple[object, ...] = ()) -> list[Row]:
        async with self._lock, self.connection.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    async def _execute(self, sql: str, params: tuple[object, ...] = ()) -> int:
        """Выполнить запись и вернуть ``cursor.rowcount``."""
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
        """Создать запись пользователя или обновить его профиль.

        ``consent_given`` при обновлении не трогается: согласие даётся один раз
        и снимается только по явному запросу пользователя.
        """
        await self._execute(
            """
            INSERT INTO users (user_id, username, first_name, consent_given, created_at)
            VALUES (?, ?, ?, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name
            """,
            (user_id, username, first_name, self.now()),
        )
        user = await self.get_user(user_id)
        if user is None:  # теоретически невозможно, но типизация требует проверки
            raise RuntimeError(f"Не удалось прочитать пользователя {user_id} после записи")
        return user

    async def get_user(self, user_id: int) -> User | None:
        row = await self._fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return User.from_row(row) if row is not None else None

    async def has_consent(self, user_id: int) -> bool:
        """Проверить согласие на обработку ПДн (152-ФЗ)."""
        row = await self._fetch_one(
            "SELECT consent_given FROM users WHERE user_id = ?",
            (user_id,),
        )
        return bool(row is not None and row["consent_given"])

    async def set_consent(self, user_id: int, granted: bool = True) -> None:
        """Зафиксировать (или отозвать) согласие на обработку ПДн."""
        await self.upsert_user(user_id)
        await self._execute(
            "UPDATE users SET consent_given = ? WHERE user_id = ?",
            (1 if granted else 0, user_id),
        )
        logger.info("Пользователь %s: consent_given=%s", user_id, granted)

    async def revoke_consent(self, user_id: int) -> None:
        """Отозвать согласие (по запросу пользователя в рамках 152-ФЗ)."""
        await self.set_consent(user_id, granted=False)

    async def delete_user(self, user_id: int) -> bool:
        """Полностью удалить пользователя и его заявки (исполнение отзыва ПДн)."""
        affected = await self._execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        logger.info("Удаление пользователя %s: затронуто строк %s", user_id, affected)
        return affected > 0

    async def count_users(self) -> tuple[int, int]:
        """Вернуть ``(всего пользователей, дали согласие)``."""
        row = await self._fetch_one(
            "SELECT COUNT(*) AS total, COALESCE(SUM(consent_given), 0) AS consented FROM users"
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
    ) -> Booking:
        """Сохранить заявку с чеком и вернуть её вместе с присвоенным ``id``."""
        if status not in BookingStatus.ALL:
            raise ValueError(f"Недопустимый статус брони: {status!r}")

        cursor_id: int | None = None
        async with self._lock:
            cursor = await self.connection.execute(
                """
                INSERT INTO bookings
                    (user_id, tariff, time_slot, client_name, receipt_file_id, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, tariff, time_slot, client_name, receipt_file_id, status, self.now()),
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

    async def get_booking(self, booking_id: int) -> Booking | None:
        row = await self._fetch_one("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        return Booking.from_row(row) if row is not None else None

    async def set_status_if_pending(
        self,
        booking_id: int,
        status: str,
        decided_by: int | None = None,
    ) -> bool:
        """Перевести заявку в новый статус, только если она ещё ``pending``.

        Вместе со статусом атомарно пишутся ``decided_by`` (кто из
        администраторов принял решение) и ``decided_at``.

        Возвращает ``True``, если статус изменён этим вызовом, и ``False``, если
        заявку обработали раньше — это защита и от двойного нажатия одним
        админом, и от гонки, когда кнопки жмут два администратора одновременно.
        """
        if status not in (BookingStatus.APPROVED, BookingStatus.REJECTED):
            raise ValueError(f"Статус после решения должен быть approved/rejected, дано: {status!r}")

        affected = await self._execute(
            """
            UPDATE bookings
               SET status = ?, decided_by = ?, decided_at = ?
             WHERE id = ? AND status = 'pending'
            """,
            (status, decided_by, self.now(), booking_id),
        )
        return affected == 1

    # ------------------------------------------------------------------ #
    # admins — несколько администраторов клуба
    # ------------------------------------------------------------------ #
    async def add_admin(
        self,
        user_id: int,
        role: str = AdminRole.OPERATOR,
        added_by: int | None = None,
    ) -> Admin:
        """Добавить администратора или включить/обновить уже существующего."""
        if role not in AdminRole.ALL:
            raise ValueError(f"Недопустимая роль администратора: {role!r}")

        await self._execute(
            """
            INSERT INTO admins (user_id, role, is_active, added_by, created_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                role      = excluded.role,
                is_active = 1,
                added_by  = COALESCE(excluded.added_by, admins.added_by)
            """,
            (user_id, role, added_by, self.now()),
        )
        admin = await self.get_admin(user_id)
        if admin is None:
            raise RuntimeError(f"Не удалось прочитать администратора {user_id} после записи")
        logger.info("Администратор %s сохранён (роль %s)", user_id, role)
        return admin

    async def get_admin(self, user_id: int) -> Admin | None:
        row = await self._fetch_one("SELECT * FROM admins WHERE user_id = ?", (user_id,))
        return Admin.from_row(row) if row is not None else None

    async def is_active_admin(self, user_id: int) -> bool:
        """Проверка доступа: администратор существует и не отключён."""
        row = await self._fetch_one(
            "SELECT is_active FROM admins WHERE user_id = ?",
            (user_id,),
        )
        return bool(row is not None and row["is_active"])

    async def list_admins(self, *, active_only: bool = False) -> list[Admin]:
        """Список администраторов: сначала владельцы, затем по дате добавления."""
        sql = (
            "SELECT * FROM admins "
            "WHERE is_active = 1 "
            if active_only
            else "SELECT * FROM admins "
        )
        rows = await self._fetch_all(
            sql
            + "ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END, created_at, user_id"
        )
        return [Admin.from_row(row) for row in rows]

    async def active_admin_ids(self) -> tuple[int, ...]:
        """ID активных администраторов — получатели уведомлений о новых бронях."""
        rows = await self._fetch_all(
            "SELECT user_id FROM admins WHERE is_active = 1 "
            "ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END, created_at, user_id"
        )
        return tuple(int(row["user_id"]) for row in rows)

    async def set_admin_active(self, user_id: int, active: bool) -> bool:
        """Отключить/включить администратора, не удаляя его из базы."""
        affected = await self._execute(
            "UPDATE admins SET is_active = ? WHERE user_id = ?",
            (1 if active else 0, user_id),
        )
        return affected == 1

    async def set_admin_role(self, user_id: int, role: str) -> bool:
        """Изменить роль администратора (``owner`` / ``operator``)."""
        if role not in AdminRole.ALL:
            raise ValueError(f"Недопустимая роль администратора: {role!r}")
        affected = await self._execute(
            "UPDATE admins SET role = ? WHERE user_id = ?",
            (role, user_id),
        )
        return affected == 1

    async def count_active_owners(self) -> int:
        """Сколько сейчас активных владельцев — защита от потери управления."""
        row = await self._fetch_one(
            "SELECT COUNT(*) AS n FROM admins WHERE role = ? AND is_active = 1",
            (AdminRole.OWNER,),
        )
        return int(row["n"]) if row is not None else 0

    async def remove_admin(self, user_id: int) -> bool:
        """Удалить администратора из базы (клиентские данные пользователя не трогаем)."""
        affected = await self._execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        logger.info("Администратор %s удалён: %s", user_id, affected == 1)
        return affected == 1

    async def count_admins(self) -> tuple[int, int]:
        """Вернуть ``(всего администраторов, активных)``."""
        row = await self._fetch_one(
            "SELECT COUNT(*) AS total, COALESCE(SUM(is_active), 0) AS active FROM admins"
        )
        if row is None:
            return 0, 0
        return int(row["total"]), int(row["active"])

    async def sync_env_admins(self, admin_ids: tuple[int, ...] | list[int]) -> None:
        """Сверить таблицу ``admins`` со списком из ``.env`` (вызывается при старте).

        * первый ID становится владельцем (``owner``) и всегда активен — его
          нельзя удалить из панели;
        * остальные ID из ``.env`` добавляются администраторами (``operator``),
          если их ещё нет, и включаются, если были отключены;
        * администраторы, добавленные через панель, не трогаются — ``.env``
          задаёт только стартовый (обязательный) набор.
        """
        for index, admin_id in enumerate(admin_ids):
            existing = await self.get_admin(admin_id)
            if index == 0:
                await self.add_admin(admin_id, role=AdminRole.OWNER, added_by=None)
            elif existing is None:
                await self.add_admin(admin_id, role=AdminRole.OPERATOR, added_by=None)
            else:
                await self.set_admin_active(admin_id, True)

    # ------------------------------------------------------------------ #
    # booking_notifications — копии уведомлений у каждого администратора
    # ------------------------------------------------------------------ #
    async def add_notification(self, booking_id: int, admin_id: int, message_id: int) -> None:
        """Запомнить, какому админу и каким сообщением отправлена заявка."""
        await self._execute(
            """
            INSERT OR IGNORE INTO booking_notifications
                (booking_id, admin_id, message_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (booking_id, admin_id, message_id, self.now()),
        )

    async def list_notifications(self, booking_id: int) -> list[tuple[int, int]]:
        """Пары ``(admin_id, message_id)`` — кому отправлялась эта заявка."""
        rows = await self._fetch_all(
            "SELECT admin_id, message_id FROM booking_notifications WHERE booking_id = ? "
            "ORDER BY id",
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
    ) -> list[Booking]:
        """Список заявок с фильтрацией по статусу и пагинацией."""
        order = "ASC" if oldest_first else "DESC"
        if status is None:
            rows = await self._fetch_all(
                f"SELECT * FROM bookings ORDER BY id {order} LIMIT ? OFFSET ?",  # noqa: S608
                (limit, offset),
            )
        else:
            rows = await self._fetch_all(
                f"SELECT * FROM bookings WHERE status = ? ORDER BY id {order} LIMIT ? OFFSET ?",  # noqa: S608
                (status, limit, offset),
            )
        return [Booking.from_row(row) for row in rows]

    async def list_bookings_with_users(
        self,
        status: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
        oldest_first: bool = False,
    ) -> list[tuple[Booking, User | None]]:
        """Заявки вместе с профилями клиентов (LEFT JOIN — одним запросом).

        Используется списком «Активные брони» в админ-панели, чтобы показывать
        @username клиента и не делать отдельный запрос на каждую заявку.
        """
        order = "ASC" if oldest_first else "DESC"
        where = "WHERE b.status = ?" if status is not None else ""
        params: tuple[object, ...] = (
            (status, limit, offset) if status is not None else (limit, offset)
        )
        rows = await self._fetch_all(
            f"""
            SELECT b.*, u.user_id AS u_id, u.username AS u_username, u.first_name AS u_first_name
              FROM bookings b
              LEFT JOIN users u ON u.user_id = b.user_id
              {where}
             ORDER BY b.id {order}
             LIMIT ? OFFSET ?
            """,  # noqa: S608 — статус и сортировка не подставляются из пользовательского ввода
            params,
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
                if row["u_id"] is not None
                else None
            )
            result.append((Booking.from_row(row), user))
        return result

    async def list_user_bookings(self, user_id: int, *, limit: int = 10) -> list[Booking]:
        """История заявок конкретного клиента."""
        rows = await self._fetch_all(
            "SELECT * FROM bookings WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return [Booking.from_row(row) for row in rows]

    async def count_bookings(self, status: str | None = None) -> int:
        if status is None:
            row = await self._fetch_one("SELECT COUNT(*) AS n FROM bookings")
        else:
            row = await self._fetch_one(
                "SELECT COUNT(*) AS n FROM bookings WHERE status = ?", (status,)
            )
        return int(row["n"]) if row is not None else 0

    async def list_tables(self) -> list[str]:
        """Список таблиц базы — используется диагностикой (``--init-db``)."""
        rows = await self._fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        return [str(row["name"]) for row in rows]

    async def get_stats(self) -> BookingStats:
        """Сводка для /admin: заявки по статусам + пользователи."""
        rows = await self._fetch_all("SELECT status, COUNT(*) AS n FROM bookings GROUP BY status")
        by_status: dict[str, int] = {str(row["status"]): int(row["n"]) for row in rows}
        users_total, users_consented = await self.count_users()
        admins_total, admins_active = await self.count_admins()

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
        )


async def init_database(path: str | Path, timezone: ZoneInfo) -> Database:
    """Создать и открыть базу данных (используется при старте и в ``--init-db``).

    Если открытие не удалось, соединение закрывается: иначе поток aiosqlite
    остаётся живым и процесс не завершается.
    """
    database = Database(path, timezone=timezone)
    try:
        await database.connect()
    except BaseException:
        await database.close()
        raise
    return database


__all__ = ["BOOKINGS_MIGRATIONS", "POST_MIGRATION_INDEXES", "Database", "init_database"]
