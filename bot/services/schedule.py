"""Расписание работы клуба, генератор слотов, буфер уборки и подбор альтернатив.

Отвечает за:
* рабочие часы по дням недели (``schedule_days``) и праздничным датам (``schedule_holidays``);
* шаг слота (``30`` или ``60`` минут), лимит гостей на слот и буфер между сессиями (10–15 мин);
* блокировку слотов вне рабочих часов, с нарушением горизонта записи или занятых
  (с учётом активных броней бота и внешних слотов Sheets / IZI);
* подбор ближайших свободных окон (например, «19:00 или 21:00»), когда гостю
  отклоняют или отменяют бронь.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from bot.db.database import Database
from bot.db.models import BookingStatus

WEEKDAY_SHORT: tuple[str, ...] = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")
WEEKDAY_FULL: tuple[str, ...] = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)

_HHMM_RE = re.compile(r"\b([01]?\d|2[0-4]):([0-5]\d)\b")
_RANGE_RE = re.compile(
    r"\b([01]?\d|2[0-4]):([0-5]\d)\s*[–—-]\s*([01]?\d|2[0-4]):([0-5]\d)\b"
)
_HOURS_COUNT_RE = re.compile(r"на\s+(\d{1,2})\s*ч", re.IGNORECASE)
_DATE_DM_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\b")


@dataclass(frozen=True, slots=True)
class SlotAvailability:
    """Итог проверки доступности конкретного временного интервала."""

    available: bool
    reason: str = ""
    seats_left: int = 0
    alternatives: tuple[str, ...] = ()


def parse_hhmm(value: str | None) -> int | None:
    """Перевести ``ЧЧ:ММ`` в минуты от полуночи (``0..1440``)."""
    if not value:
        return None
    raw = value.strip()
    if raw == "24:00":
        return 1440
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", raw)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def minutes_to_hhmm(minutes: int) -> str:
    """Перевести минуты от полуночи в ``ЧЧ:ММ``."""
    norm = minutes % 1440
    if minutes == 1440:
        return "24:00"
    return f"{norm // 60:02d}:{norm % 60:02d}"


def parse_hours_range(raw: str) -> tuple[str, str] | None:
    """Разобрать строку вида ``10:00-23:00`` или ``00:00–24:00``."""
    cleaned = (raw or "").strip().replace("—", "-").replace("–", "-")
    if "-" not in cleaned:
        return None
    left, right = [part.strip() for part in cleaned.split("-", 1)]
    start_m = parse_hhmm(left)
    end_m = parse_hhmm(right)
    if start_m is None or end_m is None or start_m == end_m:
        return None
    return (
        "24:00" if left == "24:00" else minutes_to_hhmm(start_m),
        "24:00" if right == "24:00" else minutes_to_hhmm(end_m),
    )


def extract_slot_window(
    time_slot: str,
    ref_dt: datetime,
    *,
    default_minutes: int = 60,
) -> tuple[str, str | None, str | None]:
    """Извлечь ``(YYYY-MM-DD, start_hhmm, end_hhmm)`` из текстового слота брони."""
    lowered = (time_slot or "").lower()
    target_date = ref_dt.date()
    if "завтра" in lowered:
        target_date = target_date + timedelta(days=1)
    elif "послезавтра" in lowered:
        target_date = target_date + timedelta(days=2)
    else:
        dm = _DATE_DM_RE.search(time_slot or "")
        if dm:
            day_i, month_i = int(dm.group(1)), int(dm.group(2))
            year_i = int(dm.group(3)) if dm.group(3) else ref_dt.year
            try:
                target_date = date(year_i, month_i, day_i)
            except ValueError:
                target_date = ref_dt.date()

    rng = _RANGE_RE.search(time_slot or "")
    if rng:
        s_h, s_m, e_h, e_m = (int(rng.group(i)) for i in range(1, 5))
        start_str = f"{s_h:02d}:{s_m:02d}" if s_h < 24 else "24:00"
        end_str = f"{e_h:02d}:{e_m:02d}" if e_h < 24 else "24:00"
        return target_date.isoformat(), start_str, end_str

    single = _HHMM_RE.search(time_slot or "")
    if single:
        s_min = int(single.group(1)) * 60 + int(single.group(2))
        if s_min >= 1440:
            s_min = 0
        dur_match = _HOURS_COUNT_RE.search(time_slot or "")
        duration = (
            int(dur_match.group(1)) * 60 if dur_match else max(30, default_minutes)
        )
        e_min = s_min + duration
        end_str = "24:00" if e_min == 1440 else minutes_to_hhmm(e_min)
        return target_date.isoformat(), minutes_to_hhmm(s_min), end_str

    return target_date.isoformat(), None, None


async def resolve_working_hours(
    db: Database,
    iso_date: str,
) -> tuple[str, str, bool, str]:
    """Вернуть ``(open_time, close_time, is_closed, label)`` для конкретной даты."""
    holiday = await db.get_holiday_for_date(iso_date)
    if holiday is not None:
        return (
            holiday.open_time,
            holiday.close_time,
            holiday.is_closed,
            f"праздник «{holiday.title}»",
        )
    try:
        dt = date.fromisoformat(iso_date)
    except ValueError:
        return "00:00", "24:00", False, "обычный режим"
    day_cfg = await db.get_schedule_day(dt.weekday())
    return (
        day_cfg.open_time,
        day_cfg.close_time,
        day_cfg.is_closed,
        WEEKDAY_SHORT[dt.weekday()],
    )


def _interval_minutes(start_hhmm: str, end_hhmm: str) -> tuple[int, int]:
    s = parse_hhmm(start_hhmm)
    e = parse_hhmm(end_hhmm)
    if s is None:
        s = 0
    if e is None:
        e = s + 60
    if e <= s:
        e += 1440
    return s, e


def _fits_working_hours(
    slot_s: int,
    slot_e: int,
    open_hhmm: str,
    close_hhmm: str,
) -> bool:
    open_m = parse_hhmm(open_hhmm)
    close_m = parse_hhmm(close_hhmm)
    if open_m is None or close_m is None:
        return True
    if open_m == 0 and close_m == 1440:
        return True
    work_s, work_e = _interval_minutes(open_hhmm, close_hhmm)
    if slot_s >= work_s and slot_e <= work_e:
        return True
    # Если смена клуба переходит через полночь (например 14:00–04:00), а слот 01:00–03:00
    return bool(work_e > 1440 and slot_s + 1440 >= work_s and slot_e + 1440 <= work_e)


async def schedule_parameters(db: Database) -> tuple[int, int, int, int]:
    """Вернуть ``(step_minutes, max_guests, buffer_minutes, lead_hours)``."""
    settings = await db.get_settings()
    try:
        step = int(settings.get("slot_step_minutes") or 60)
    except ValueError:
        step = 60
    step = 30 if step <= 30 else 60

    try:
        max_guests = max(1, int(settings.get("slot_max_guests") or 16))
    except ValueError:
        max_guests = 16

    try:
        buffer_min = max(0, int(settings.get("slot_buffer_minutes") or 15))
    except ValueError:
        buffer_min = 15

    try:
        lead_hours = max(0, int(settings.get("booking_rules_lead_hours") or 0))
    except ValueError:
        lead_hours = 0

    return step, max_guests, buffer_min, lead_hours


async def _occupied_seats(
    db: Database,
    iso_date: str,
    slot_s: int,
    slot_e: int,
    *,
    branch_id: int = 1,
    buffer_min: int = 15,
    exclude_booking_id: int | None = None,
) -> int:
    """Посчитать занятые места на пересекающихся бронях и внешних слотах с учётом буфера."""
    used = 0
    bookings = await db.list_bookings(branch_id=branch_id, for_date=iso_date, limit=500)
    for b in bookings:
        if exclude_booking_id is not None and b.id == exclude_booking_id:
            continue
        if b.status not in (BookingStatus.PENDING, BookingStatus.APPROVED):
            continue
        if not b.slot_start or not b.slot_end:
            continue
        b_s, b_e = _interval_minutes(b.slot_start, b.slot_end)
        # С учётом буфера на уборку между сессиями
        if slot_s < (b_e + buffer_min) and (slot_e + buffer_min) > b_s:
            used += max(1, b.guests_count)

    ext_slots = await db.list_external_busy_slots(
        slot_date=iso_date, branch_id=branch_id
    )
    for ext in ext_slots:
        e_s, e_e = _interval_minutes(ext.start_time, ext.end_time)
        if slot_s < (e_e + buffer_min) and (slot_e + buffer_min) > e_s:
            used += max(1, ext.seats_taken)

    return used


async def generate_day_slots(
    db: Database,
    iso_date: str,
    *,
    branch_id: int = 1,
    guests_count: int = 1,
    now_dt: datetime | None = None,
) -> list[tuple[str, str, int]]:
    """Сгенерировать доступные слоты ``(start_hhmm, end_hhmm, seats_left)`` на дату."""
    open_t, close_t, is_closed, _ = await resolve_working_hours(db, iso_date)
    if is_closed:
        return []

    step, max_guests, buffer_min, lead_hours = await schedule_parameters(db)
    work_s, work_e = _interval_minutes(open_t, close_t)

    min_allowed_min: int | None = None
    if now_dt is not None and iso_date == now_dt.date().isoformat() and lead_hours > 0:
        cutoff = now_dt + timedelta(hours=lead_hours)
        if cutoff.date() > now_dt.date():
            return []
        min_allowed_min = cutoff.hour * 60 + cutoff.minute

    available: list[tuple[str, str, int]] = []
    cur = work_s
    while cur + step <= work_e:
        nxt = cur + step
        if min_allowed_min is not None and cur < min_allowed_min:
            cur += step
            continue
        used = await _occupied_seats(
            db, iso_date, cur, nxt, branch_id=branch_id, buffer_min=buffer_min
        )
        seats_left = max_guests - used
        if seats_left >= guests_count:
            start_lbl = minutes_to_hhmm(cur)
            end_lbl = "24:00" if nxt == 1440 else minutes_to_hhmm(nxt)
            available.append((start_lbl, end_lbl, seats_left))
        cur += step
    return available


async def suggest_alternatives(
    db: Database,
    iso_date: str,
    desired_start: str | None,
    *,
    branch_id: int = 1,
    guests_count: int = 1,
    count: int = 2,
) -> tuple[str, ...]:
    """Подобрать ближайшие свободные слоты (например, ``('19:00', '21:00')``)."""
    slots = await generate_day_slots(
        db, iso_date, branch_id=branch_id, guests_count=guests_count
    )
    if not slots:
        return ()

    target_m = parse_hhmm(desired_start) if desired_start else 20 * 60
    if target_m is None:
        target_m = 20 * 60

    candidates = [
        (abs((parse_hhmm(s) or 0) - target_m), s)
        for s, _e, _left in slots
        if s != desired_start
    ]
    candidates.sort(key=lambda item: (item[0], item[1]))
    picked = [s for _dist, s in candidates[:count]]
    picked.sort()
    return tuple(picked)


async def check_slot_availability(
    db: Database,
    *,
    iso_date: str,
    start_hhmm: str | None,
    end_hhmm: str | None,
    branch_id: int = 1,
    guests_count: int = 1,
    now_dt: datetime | None = None,
    exclude_booking_id: int | None = None,
    is_instant_now: bool = False,
) -> SlotAvailability:
    """Проверить, можно ли забронировать указанный интервал."""
    open_t, close_t, is_closed, day_label = await resolve_working_hours(db, iso_date)
    if is_closed:
        return SlotAvailability(
            available=False,
            reason=f"В выбранный день ({day_label}) клуб закрыт.",
        )

    _step, max_guests, buffer_min, lead_hours = await schedule_parameters(db)
    if guests_count > max_guests:
        return SlotAvailability(
            available=False,
            reason=f"Максимум гостей в одном слоте — {max_guests} чел.",
        )

    if start_hhmm is None or end_hhmm is None:
        return SlotAvailability(available=True, seats_left=max_guests)

    slot_s, slot_e = _interval_minutes(start_hhmm, end_hhmm)
    if not _fits_working_hours(slot_s, slot_e, open_t, close_t):
        alts = await suggest_alternatives(
            db, iso_date, start_hhmm, branch_id=branch_id, guests_count=guests_count
        )
        return SlotAvailability(
            available=False,
            reason=f"Время вне рабочих часов клуба ({open_t}–{close_t}, {day_label}).",
            alternatives=alts,
        )

    if not is_instant_now and lead_hours > 0 and now_dt is not None:
        try:
            slot_dt = datetime.combine(
                date.fromisoformat(iso_date),
                datetime.min.time(),
                tzinfo=now_dt.tzinfo,
            ) + timedelta(minutes=slot_s)
            if slot_dt < now_dt + timedelta(hours=lead_hours):
                alts = await suggest_alternatives(
                    db,
                    iso_date,
                    minutes_to_hhmm((now_dt.hour + lead_hours) * 60 + now_dt.minute),
                    branch_id=branch_id,
                    guests_count=guests_count,
                )
                return SlotAvailability(
                    available=False,
                    reason=f"Бронирование открыто минимум за {lead_hours} ч до визита.",
                    alternatives=alts,
                )
        except ValueError:
            pass

    used = await _occupied_seats(
        db,
        iso_date,
        slot_s,
        slot_e,
        branch_id=branch_id,
        buffer_min=buffer_min,
        exclude_booking_id=exclude_booking_id,
    )
    seats_left = max_guests - used
    if seats_left < guests_count:
        alts = await suggest_alternatives(
            db, iso_date, start_hhmm, branch_id=branch_id, guests_count=guests_count
        )
        return SlotAvailability(
            available=False,
            reason=(
                f"Слот {start_hhmm}–{end_hhmm} занят (с учётом {buffer_min} мин на уборку "
                f"свободно {max(0, seats_left)} из {max_guests} мест)."
            ),
            seats_left=max(0, seats_left),
            alternatives=alts,
        )

    return SlotAvailability(available=True, seats_left=seats_left)


__all__ = [
    "WEEKDAY_FULL",
    "WEEKDAY_SHORT",
    "SlotAvailability",
    "check_slot_availability",
    "extract_slot_window",
    "generate_day_slots",
    "minutes_to_hhmm",
    "parse_hhmm",
    "parse_hours_range",
    "resolve_working_hours",
    "schedule_parameters",
    "suggest_alternatives",
]
