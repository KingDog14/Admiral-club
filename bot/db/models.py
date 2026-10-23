"""Модели данных и константы статусов брони."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from sqlite3 import Row

from bot.utils.formatting import user_link


def _column(row: Row, name: str, default: object = None) -> object:
    """Безопасно прочитать столбец: старые базы могли быть созданы до миграции."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


class BookingStatus:
    """Статусы заявки (ровно те, что хранятся в столбце ``bookings.status``)."""

    PENDING: str = "pending"
    APPROVED: str = "approved"
    REJECTED: str = "rejected"

    ALL: tuple[str, ...] = (PENDING, APPROVED, REJECTED)

    TITLES: dict[str, str] = {
        PENDING: "ожидает подтверждения",
        APPROVED: "подтверждена",
        REJECTED: "отклонена",
    }

    DECISION_TITLES: dict[str, str] = {
        APPROVED: "подтвердил",
        REJECTED: "отклонил",
    }

    @classmethod
    def title(cls, status: str) -> str:
        """Человекочитаемое название статуса для сообщений администратору."""
        return cls.TITLES.get(status, status)

    @classmethod
    def decision_title(cls, status: str) -> str:
        """Глагол решения: «подтвердил» / «отклонил»."""
        return cls.DECISION_TITLES.get(status, "обработал")


class AdminRole:
    """Роли администраторов клуба."""

    OWNER: str = "owner"
    OPERATOR: str = "operator"

    ALL: tuple[str, ...] = (OWNER, OPERATOR)

    TITLES: dict[str, str] = {
        OWNER: "владелец",
        OPERATOR: "администратор",
    }

    @classmethod
    def title(cls, role: str) -> str:
        return cls.TITLES.get(role, role)


@dataclass(frozen=True, slots=True)
class Admin:
    """Строка таблицы ``admins`` — администратор клуба."""

    user_id: int
    role: str = AdminRole.OPERATOR
    is_active: bool = True
    added_by: int | None = None
    created_at: str = ""

    @classmethod
    def from_row(cls, row: Row) -> Admin:
        return cls(
            user_id=int(row["user_id"]),
            role=str(row["role"] or AdminRole.OPERATOR),
            is_active=bool(row["is_active"]),
            added_by=(int(row["added_by"]) if row["added_by"] is not None else None),
            created_at=str(row["created_at"] or ""),
        )

    @property
    def is_owner(self) -> bool:
        return self.role == AdminRole.OWNER

    @property
    def role_title(self) -> str:
        return AdminRole.title(self.role)


@dataclass(frozen=True, slots=True)
class User:
    """Строка таблицы ``users``."""

    user_id: int
    username: str | None
    first_name: str | None
    consent_given: bool
    created_at: str

    @classmethod
    def from_row(cls, row: Row) -> User:
        return cls(
            user_id=int(row["user_id"]),
            username=row["username"],
            first_name=row["first_name"],
            consent_given=bool(row["consent_given"]),
            created_at=str(row["created_at"] or ""),
        )

    @property
    def display_name(self) -> str:
        """Имя для приветствия: имя из профиля, иначе username, иначе «гость»."""
        return self.first_name or self.username or "гость"

    @property
    def mention(self) -> str:
        """Короткое имя для служебных подписей: «@username» или «Имя»."""
        return f"@{self.username}" if self.username else (self.first_name or f"id{self.user_id}")

    @property
    def link(self) -> str:
        """HTML-ссылка/идентификатор пользователя для админ-панели."""
        return user_link(self.user_id, self.username, self.first_name)


@dataclass(frozen=True, slots=True)
class Booking:
    """Строка таблицы ``bookings``."""

    id: int
    user_id: int
    tariff: str
    time_slot: str
    client_name: str
    receipt_file_id: str | None
    status: str
    created_at: str
    decided_by: int | None = None
    decided_at: str | None = None

    @classmethod
    def from_row(cls, row: Row) -> Booking:
        decided_by = _column(row, "decided_by")
        decided_at = _column(row, "decided_at")
        return cls(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            tariff=str(row["tariff"]),
            time_slot=str(row["time_slot"]),
            client_name=str(row["client_name"]),
            receipt_file_id=row["receipt_file_id"],
            status=str(row["status"]),
            created_at=str(row["created_at"] or ""),
            decided_by=int(decided_by) if decided_by is not None else None,
            decided_at=str(decided_at) if decided_at is not None else None,
        )

    @property
    def is_pending(self) -> bool:
        return self.status == BookingStatus.PENDING

    @property
    def is_approved(self) -> bool:
        return self.status == BookingStatus.APPROVED


@dataclass(frozen=True, slots=True)
class BookingStats:
    """Сводка для админ-панели (команда /admin)."""

    total: int
    pending: int
    approved: int
    rejected: int
    users: int
    consented: int
    admins: int = field(default=0)
    admins_active: int = field(default=0)

    def as_dict(self) -> dict[str, int]:
        """Словарь для подстановки в ``texts.STATS_TEMPLATE.format(**stats)``."""
        return asdict(self)


__all__ = ["Admin", "AdminRole", "Booking", "BookingStats", "BookingStatus", "User"]
