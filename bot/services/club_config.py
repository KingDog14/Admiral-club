"""Живые настройки клуба, точки, расчёт стоимости с доп. услугами и права ролей."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date

from bot import texts
from bot.config import AppConfig, ClubInfo
from bot.db.database import Database
from bot.db.models import Admin, AdminRole, TariffExtra, TariffRecord

_HOURS_RE = re.compile(r"(?:на\s*)?(\d{1,2})\s*ч", re.IGNORECASE)


async def effective_club_info(
    db: Database,
    cfg: AppConfig,
    *,
    branch_id: int | None = None,
) -> tuple[ClubInfo, dict[str, str]]:
    """Собрать актуальный :class:`ClubInfo` с учётом ``club_settings`` и выбранной точки."""
    settings = await db.get_settings()
    base = cfg.club

    name = settings.get("club_name") or base.name
    city = settings.get("club_city") or base.city
    address = settings.get("club_address") or base.address
    hours = settings.get("club_hours") or base.hours
    phone = settings.get("admin_phone") or base.admin_phone
    pay_text = settings.get("payment_text") or ""

    if branch_id is not None:
        branch = await db.get_branch(branch_id)
        if branch is not None:
            if branch.city:
                city = branch.city
            if branch.address:
                address = branch.address
            if branch.hours:
                hours = branch.hours
            if branch.phone:
                phone = branch.phone

    pay_sys = settings.get("payment_system") or base.payment_system
    pay_card = settings.get("payment_card") or pay_text or base.payment_card
    pay_holder = settings.get("payment_holder") or base.payment_holder
    club = ClubInfo(
        name=name,
        city=city,
        address=address,
        hours=hours,
        admin_name=base.admin_name,
        admin_phone=phone,
        payment_system=pay_sys,
        payment_card=pay_card,
        payment_holder=pay_holder,
        bot_contact=base.bot_contact,
    )
    return club, settings


def admin_branch_scope(admin: Admin | None) -> int | None:
    """Определить область видимости администратора по точкам.

    * ``owner`` и ``viewer`` (без жёсткой привязки) видят все точки (``None``);
    * ``operator`` (``admin``) с указанным ``branch_id`` видит только свою точку.
    """
    if admin is None:
        return None
    if admin.role == AdminRole.OWNER:
        return None
    if admin.branch_id is not None:
        return admin.branch_id
    return None


async def calculate_quote(
    db: Database,
    *,
    tariff_key_or_title: str,
    time_key_or_slot: str,
    day: date,
    guests_count: int = 1,
    extra_keys: Sequence[str] = (),
) -> tuple[int | None, str]:
    """Посчитать предварительную стоимость бронирования (тариф будни/выходные + доп. услуги).

    Возвращает ``(сумма_в_рублях | None, человекочитаемое_описание)``.
    """
    rec: TariffRecord | None = await db.get_tariff_record(tariff_key_or_title)
    weekend = texts.is_weekend(day)
    guests = max(1, int(guests_count))

    extras_sum = 0
    extra_labels: list[str] = []
    if extra_keys:
        all_extras: dict[str, TariffExtra] = {
            e.key: e for e in await db.list_tariff_extras(active_only=False)
        }
        for key in extra_keys:
            item = all_extras.get(key)
            if item is not None:
                extras_sum += item.price
                extra_labels.append(
                    f"{item.title} (+{item.price} ₽)" if item.price else item.title
                )

    if rec is None:
        legacy = texts.amount_for_booking(tariff_key_or_title, time_key_or_slot, day)
        if legacy is None:
            return (
                extras_sum if extras_sum > 0 else None,
                "по прайсу" + (f" + доп. услуги {extras_sum} ₽" if extras_sum else ""),
            )
        total = legacy * guests + extras_sum
        return total, f"{total} ₽"

    lowered = (time_key_or_slot or "").lower()
    base_per_guest: int | None = None
    pack_label = ""

    if (
        lowered == texts.TIME_NIGHT_KEY
        or "ноч" in lowered
        or texts.NIGHT_SLOT in time_key_or_slot
    ):
        base_per_guest = rec.night_for(weekend)
        pack_label = "ночь (ПТ–ВС)" if weekend else "ночь (будни)"
    elif (
        lowered == texts.TIME_MORNING_KEY
        or "утр" in lowered
        or texts.MORNING_SLOT in time_key_or_slot
    ):
        base_per_guest = rec.morning
        pack_label = "утро"
    elif (
        lowered == texts.TIME_DAY_KEY
        or "ден" in lowered
        or texts.DAY_SLOT in time_key_or_slot
    ):
        base_per_guest = rec.day
        pack_label = "день"
    else:
        hm = _HOURS_RE.search(lowered)
        if hm:
            hrs = int(hm.group(1))
            if hrs == 3:
                base_per_guest = rec.hour3_for(weekend)
                pack_label = "3 ч"
            elif hrs == 5:
                base_per_guest = rec.hour5_for(weekend)
                pack_label = "5 ч"
            elif hrs > 0:
                base_per_guest = rec.hour1_for(weekend) * hrs
                pack_label = f"{hrs} ч"

    if base_per_guest is None:
        rate = rec.hour1_for(weekend)
        hint = f"от {rate} ₽/ч ({'выходные' if weekend else 'будни'})"
        if guests > 1:
            hint += f" × {guests} чел."
        if extra_labels:
            hint += f" + {', '.join(extra_labels)}"
        return None, hint

    total = base_per_guest * guests + extras_sum
    parts = [f"{base_per_guest} ₽ ({pack_label})"]
    if guests > 1:
        parts[0] += f" × {guests} чел."
    if extra_labels:
        parts.extend(extra_labels)
    breakdown = f"<b>{total} ₽</b> ({' + '.join(parts)})"
    return total, breakdown


__all__ = [
    "admin_branch_scope",
    "calculate_quote",
    "effective_club_info",
]
