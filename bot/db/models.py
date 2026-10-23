"""Модели данных и константы статусов брони."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from sqlite3 import Row

from bot.utils.formatting import user_link


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

    @classmethod
    def title(cls, status: str) -> str:
        """Человекочитаемое название статуса для сообщений администратору."""
        return cls.TITLES.get(status, status)


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

    @classmethod
    def from_row(cls, row: Row) -> Booking:
        return cls(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            tariff=str(row["tariff"]),
            time_slot=str(row["time_slot"]),
            client_name=str(row["client_name"]),
            receipt_file_id=row["receipt_file_id"],
            status=str(row["status"]),
            created_at=str(row["created_at"] or ""),
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

    def as_dict(self) -> dict[str, int]:
        """Словарь для подстановки в ``texts.STATS_TEMPLATE.format(**stats)``."""
        return asdict(self)


__all__ = ["Booking", "BookingStats", "BookingStatus", "User"]
