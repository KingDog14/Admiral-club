"""Модели данных, роли администраторов, причины отказов и константы статусов."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from sqlite3 import Row
from typing import ClassVar

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

    TITLES: ClassVar[dict[str, str]] = {
        PENDING: "ожидает подтверждения",
        APPROVED: "подтверждена",
        REJECTED: "отклонена",
    }

    DECISION_TITLES: ClassVar[dict[str, str]] = {
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


class RejectReason:
    """Коды и подписи причин отклонения / отмены брони."""

    NO_SEATS: str = "no_seats"
    UNPAID: str = "unpaid"
    GUEST_CANCELED: str = "guest_canceled"
    CLOSED_HOURS: str = "closed_hours"
    ADMIN_CANCELED: str = "admin_canceled"
    NO_SHOW: str = "no_show"

    TITLES: ClassVar[dict[str, str]] = {
        NO_SEATS: "нет мест",
        UNPAID: "гость не оплатил",
        GUEST_CANCELED: "отменил сам",
        CLOSED_HOURS: "вне рабочих часов",
        ADMIN_CANCELED: "отменена после подтверждения",
        NO_SHOW: "гость не пришёл",
    }

    #: Основные причины для кнопок отклонения новой заявки.
    REJECT_CHOICES: tuple[tuple[str, str], ...] = (
        (NO_SEATS, "🪑 Нет мест"),
        (UNPAID, "💳 Гость не оплатил"),
        (GUEST_CANCELED, "🙋 Отменил сам"),
        (CLOSED_HOURS, "🕒 Нерабочее время"),
    )

    #: Причины для отмены уже подтверждённой брони.
    CANCEL_CHOICES: tuple[tuple[str, str], ...] = (
        (NO_SEATS, "🪑 Техпричина / нет мест"),
        (GUEST_CANCELED, "🙋 По просьбе гостя"),
        (UNPAID, "💳 Не поступила оплата"),
        (NO_SHOW, "🚷 Гость не пришёл"),
    )

    @classmethod
    def title(cls, code: str | None) -> str:
        if not code:
            return "без указания причины"
        return cls.TITLES.get(code, code)


class AdminRole:
    """Роли администраторов клуба.

    * ``owner``    — владелец: видит всё, управляет точками, админами, тарифами и настройками;
    * ``operator`` — администратор точки (синоним ``admin``): видит свою точку и свои брони;
    * ``viewer``   — наблюдатель: только просмотр без действий (защита от случайных нажатий).
    """

    OWNER: str = "owner"
    OPERATOR: str = "operator"
    ADMIN: str = "operator"
    VIEWER: str = "viewer"

    ALL: tuple[str, ...] = (OWNER, OPERATOR, VIEWER)

    TITLES: ClassVar[dict[str, str]] = {
        OWNER: "владелец",
        OPERATOR: "администратор",
        "admin": "администратор",
        VIEWER: "наблюдатель",
    }

    @classmethod
    def normalize(cls, role: str | None) -> str:
        raw = (role or cls.OPERATOR).strip().lower()
        if raw == "admin":
            return cls.OPERATOR
        return raw if raw in cls.ALL else cls.OPERATOR

    @classmethod
    def title(cls, role: str) -> str:
        return cls.TITLES.get(role, role)


@dataclass(frozen=True, slots=True)
class Branch:
    """Точка сети клубов (таблица ``branches``)."""

    id: int
    name: str
    city: str = ""
    address: str = ""
    hours: str = "круглосуточно, 24/7"
    phone: str = ""
    is_active: bool = True
    created_at: str = ""

    @classmethod
    def from_row(cls, row: Row) -> Branch:
        return cls(
            id=int(row["id"]),
            name=str(row["name"]),
            city=str(_column(row, "city", "") or ""),
            address=str(_column(row, "address", "") or ""),
            hours=str(_column(row, "hours", "") or "круглосуточно, 24/7"),
            phone=str(_column(row, "phone", "") or ""),
            is_active=bool(_column(row, "is_active", 1)),
            created_at=str(_column(row, "created_at", "") or ""),
        )

    @property
    def full_address(self) -> str:
        if self.city and self.address:
            return f"г. {self.city}, {self.address}"
        return self.address or self.city or self.name


@dataclass(frozen=True, slots=True)
class Admin:
    """Строка таблицы ``admins`` — администратор клуба."""

    user_id: int
    role: str = AdminRole.OPERATOR
    is_active: bool = True
    added_by: int | None = None
    created_at: str = ""
    display_name: str | None = None
    branch_id: int | None = None
    notify_new: bool = True
    notify_cancel: bool = True
    notify_noshow: bool = True
    quiet_start: str | None = None
    quiet_end: str | None = None

    @classmethod
    def from_row(cls, row: Row) -> Admin:
        raw_role = str(row["role"] or AdminRole.OPERATOR)
        branch_raw = _column(row, "branch_id")
        return cls(
            user_id=int(row["user_id"]),
            role=AdminRole.normalize(raw_role),
            is_active=bool(row["is_active"]),
            added_by=(int(row["added_by"]) if row["added_by"] is not None else None),
            created_at=str(row["created_at"] or ""),
            display_name=(
                str(_column(row, "display_name"))
                if _column(row, "display_name") is not None
                else None
            ),
            branch_id=int(branch_raw) if branch_raw is not None else None,
            notify_new=bool(_column(row, "notify_new", 1)),
            notify_cancel=bool(_column(row, "notify_cancel", 1)),
            notify_noshow=bool(_column(row, "notify_noshow", 1)),
            quiet_start=(
                str(_column(row, "quiet_start"))
                if _column(row, "quiet_start")
                else None
            ),
            quiet_end=(
                str(_column(row, "quiet_end")) if _column(row, "quiet_end") else None
            ),
        )

    @property
    def is_owner(self) -> bool:
        return self.role == AdminRole.OWNER

    @property
    def is_viewer(self) -> bool:
        return self.role == AdminRole.VIEWER

    @property
    def can_act(self) -> bool:
        """Может ли совершать действия с бронями (не отключён и не viewer)."""
        return self.is_active and not self.is_viewer

    @property
    def role_title(self) -> str:
        return AdminRole.title(self.role)


@dataclass(frozen=True, slots=True)
class AdminInvite:
    """Одноразовое приглашение в администраторы."""

    token: str
    name: str
    role: str
    branch_id: int | None
    created_by: int | None
    used_by: int | None
    created_at: str
    used_at: str | None

    @classmethod
    def from_row(cls, row: Row) -> AdminInvite:
        branch_raw = _column(row, "branch_id")
        used_by = _column(row, "used_by")
        created_by = _column(row, "created_by")
        return cls(
            token=str(row["token"]),
            name=str(_column(row, "name", "") or ""),
            role=AdminRole.normalize(str(_column(row, "role", AdminRole.OPERATOR))),
            branch_id=int(branch_raw) if branch_raw is not None else None,
            created_by=int(created_by) if created_by is not None else None,
            used_by=int(used_by) if used_by is not None else None,
            created_at=str(_column(row, "created_at", "") or ""),
            used_at=str(_column(row, "used_at")) if _column(row, "used_at") else None,
        )


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
        return (
            f"@{self.username}"
            if self.username
            else (self.first_name or f"id{self.user_id}")
        )

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
    branch_id: int | None = 1
    booking_date: str | None = None
    slot_start: str | None = None
    slot_end: str | None = None
    guests_count: int = 1
    seat_label: str | None = None
    guest_comment: str | None = None
    guest_contact: str | None = None
    extras: tuple[str, ...] = ()
    quoted_price: int | None = None
    receipt_kind: str = "photo"
    reject_reason: str | None = None
    reject_comment: str | None = None
    was_approved: bool = False
    no_show: bool = False
    created_by_admin: bool = False

    @classmethod
    def from_row(cls, row: Row) -> Booking:
        decided_by = _column(row, "decided_by")
        decided_at = _column(row, "decided_at")
        branch_raw = _column(row, "branch_id", 1)
        guests_raw = _column(row, "guests_count", 1)
        quoted_raw = _column(row, "quoted_price")
        extras_raw = _column(row, "extras_json", "[]")
        parsed_extras: tuple[str, ...] = ()
        if isinstance(extras_raw, str) and extras_raw.strip():
            try:
                loaded = json.loads(extras_raw)
                if isinstance(loaded, list):
                    parsed_extras = tuple(str(x) for x in loaded if x)
            except ValueError:
                parsed_extras = ()

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
            branch_id=int(branch_raw) if branch_raw is not None else None,
            booking_date=(
                str(_column(row, "booking_date"))
                if _column(row, "booking_date")
                else None
            ),
            slot_start=(
                str(_column(row, "slot_start")) if _column(row, "slot_start") else None
            ),
            slot_end=(
                str(_column(row, "slot_end")) if _column(row, "slot_end") else None
            ),
            guests_count=max(1, int(guests_raw or 1)),
            seat_label=(
                str(_column(row, "seat_label")) if _column(row, "seat_label") else None
            ),
            guest_comment=(
                str(_column(row, "guest_comment"))
                if _column(row, "guest_comment")
                else None
            ),
            guest_contact=(
                str(_column(row, "guest_contact"))
                if _column(row, "guest_contact")
                else None
            ),
            extras=parsed_extras,
            quoted_price=int(quoted_raw) if quoted_raw is not None else None,
            receipt_kind=str(_column(row, "receipt_kind", "photo") or "photo"),
            reject_reason=(
                str(_column(row, "reject_reason"))
                if _column(row, "reject_reason")
                else None
            ),
            reject_comment=(
                str(_column(row, "reject_comment"))
                if _column(row, "reject_comment")
                else None
            ),
            was_approved=bool(_column(row, "was_approved", 0)),
            no_show=bool(_column(row, "no_show", 0)),
            created_by_admin=bool(_column(row, "created_by_admin", 0)),
        )

    @property
    def is_pending(self) -> bool:
        return self.status == BookingStatus.PENDING

    @property
    def is_approved(self) -> bool:
        return self.status == BookingStatus.APPROVED

    @property
    def reason_label(self) -> str:
        base = RejectReason.title(self.reject_reason)
        if self.reject_comment:
            return f"{base} ({self.reject_comment})"
        return base


@dataclass(frozen=True, slots=True)
class ScheduleDay:
    """Рабочие часы дня недели (0 = ПН ... 6 = ВС)."""

    weekday: int
    open_time: str = "00:00"
    close_time: str = "24:00"
    is_closed: bool = False

    @classmethod
    def from_row(cls, row: Row) -> ScheduleDay:
        return cls(
            weekday=int(row["weekday"]),
            open_time=str(row["open_time"] or "00:00"),
            close_time=str(row["close_time"] or "24:00"),
            is_closed=bool(row["is_closed"]),
        )


@dataclass(frozen=True, slots=True)
class ScheduleHoliday:
    """Режим работы в праздничный / особый день."""

    holiday_date: str
    title: str = "Праздничный день"
    open_time: str = "12:00"
    close_time: str = "24:00"
    is_closed: bool = False

    @classmethod
    def from_row(cls, row: Row) -> ScheduleHoliday:
        return cls(
            holiday_date=str(row["holiday_date"]),
            title=str(_column(row, "title", "Праздничный день") or "Праздничный день"),
            open_time=str(row["open_time"] or "12:00"),
            close_time=str(row["close_time"] or "24:00"),
            is_closed=bool(row["is_closed"]),
        )


@dataclass(frozen=True, slots=True)
class ExternalBusySlot:
    """Занятый слот из внешней таблицы (Google Sheets / IZI)."""

    id: int
    branch_id: int
    slot_date: str
    start_time: str
    end_time: str
    seats_taken: int
    source: str = "izi"
    note: str = ""

    @classmethod
    def from_row(cls, row: Row) -> ExternalBusySlot:
        return cls(
            id=int(row["id"]),
            branch_id=int(_column(row, "branch_id", 1) or 1),
            slot_date=str(row["slot_date"]),
            start_time=str(row["start_time"]),
            end_time=str(row["end_time"]),
            seats_taken=int(_column(row, "seats_taken", 16) or 16),
            source=str(_column(row, "source", "izi") or "izi"),
            note=str(_column(row, "note", "") or ""),
        )


@dataclass(frozen=True, slots=True)
class TariffRecord:
    """Динамический тариф зала в базе данных (будни + выходные)."""

    key: str
    title: str
    emoji: str
    pcs: int | None
    hour1: int
    hour1_weekend: int
    hour3: int
    hour3_weekend: int
    hour5: int
    hour5_weekend: int
    morning: int
    day: int
    night: int
    night_weekend: int
    description: str = ""
    sort_order: int = 0
    is_active: bool = True

    @classmethod
    def from_row(cls, row: Row) -> TariffRecord:
        pcs_val = _column(row, "pcs")
        return cls(
            key=str(row["key"]),
            title=str(row["title"]),
            emoji=str(_column(row, "emoji", "🖥") or "🖥"),
            pcs=int(pcs_val) if pcs_val is not None else None,
            hour1=int(row["hour1"]),
            hour1_weekend=int(_column(row, "hour1_weekend", row["hour1"])),
            hour3=int(row["hour3"]),
            hour3_weekend=int(_column(row, "hour3_weekend", row["hour3"])),
            hour5=int(row["hour5"]),
            hour5_weekend=int(_column(row, "hour5_weekend", row["hour5"])),
            morning=int(row["morning"]),
            day=int(row["day"]),
            night=int(row["night"]),
            night_weekend=int(row["night_weekend"]),
            description=str(_column(row, "description", "") or ""),
            sort_order=int(_column(row, "sort_order", 0) or 0),
            is_active=bool(_column(row, "is_active", 1)),
        )

    def hour1_for(self, weekend: bool) -> int:
        return self.hour1_weekend if weekend else self.hour1

    def hour3_for(self, weekend: bool) -> int:
        return self.hour3_weekend if weekend else self.hour3

    def hour5_for(self, weekend: bool) -> int:
        return self.hour5_weekend if weekend else self.hour5

    def night_for(self, weekend: bool) -> int:
        return self.night_weekend if weekend else self.night


@dataclass(frozen=True, slots=True)
class TariffExtra:
    """Дополнительная услуга к бронированию."""

    key: str
    title: str
    emoji: str
    price: int
    is_active: bool = True

    @classmethod
    def from_row(cls, row: Row) -> TariffExtra:
        return cls(
            key=str(row["key"]),
            title=str(row["title"]),
            emoji=str(_column(row, "emoji", "✨") or "✨"),
            price=int(_column(row, "price", 0) or 0),
            is_active=bool(_column(row, "is_active", 1)),
        )


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
    manual: int = field(default=0)
    approved_today: int = field(default=0)
    rejected_today: int = field(default=0)
    no_shows: int = field(default=0)
    branches_active: int = field(default=1)

    def as_dict(self) -> dict[str, int]:
        """Словарь для подстановки в шаблоны статистики."""
        return asdict(self)


__all__ = [
    "Admin",
    "AdminInvite",
    "AdminRole",
    "Booking",
    "BookingStats",
    "BookingStatus",
    "Branch",
    "ExternalBusySlot",
    "RejectReason",
    "ScheduleDay",
    "ScheduleHoliday",
    "TariffExtra",
    "TariffRecord",
    "User",
]
