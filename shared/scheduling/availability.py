"""
Computing when a doctor is actually free.

A slot is never stored - it is derived, on demand, from three things: the
doctor's recurring weekly hours (AvailabilityRule), today's exceptions
(AvailabilityException), and what's already booked (Appointment). Storing
slots as rows would mean rewriting a table every time a doctor's hours
change; computing them means changing an hour is one row edit.

Rules and exceptions are written and read in the business's own local time -
"10:00-13:00" means 10am Asia/Kolkata (or whatever Business.timezone holds),
not naive UTC. Appointment.starts_at is stored timezone-aware, so the
boundary between local wall-clock time and an absolute instant is crossed
exactly once, here, rather than scattered across every caller.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    Appointment,
    AppointmentStatus,
    AvailabilityException,
    AvailabilityRule,
    Business,
    Doctor,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class Slot:
    """One bookable point in time, in UTC - ready to hand straight to booking.book()."""

    starts_at: datetime
    ends_at: datetime


async def open_slots(
    db: AsyncSession,
    *,
    business: Business,
    doctor: Doctor,
    on_date: date,
    now: datetime | None = None,
) -> list[Slot]:
    """
    Every free slot for one doctor on one calendar date, business-local
    timezone, earliest first. Slots that have already passed today are
    excluded rather than offered.
    """
    tz = ZoneInfo(business.timezone)
    now = now or datetime.now(tz)

    exception = (
        await db.execute(
            select(AvailabilityException).where(
                AvailabilityException.doctor_id == doctor.id,
                AvailabilityException.date == on_date,
            )
        )
    ).scalar_one_or_none()

    if exception and exception.is_unavailable:
        return []

    candidates: list[tuple[datetime, datetime, int]] = []  # (start_local, end_local, slot_minutes)

    if exception and not exception.is_unavailable and exception.start_time and exception.end_time:
        # An added block for this date only - e.g. an extra Saturday clinic.
        # Falls back to a doctor's usual slot length if they have one on
        # record; 15 minutes otherwise.
        usual = (
            await db.execute(
                select(AvailabilityRule.slot_duration_minutes)
                .where(AvailabilityRule.doctor_id == doctor.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        candidates.append((
            datetime.combine(on_date, exception.start_time, tzinfo=tz),
            datetime.combine(on_date, exception.end_time, tzinfo=tz),
            usual or 15,
        ))
    else:
        rules = (
            await db.execute(
                select(AvailabilityRule).where(
                    AvailabilityRule.doctor_id == doctor.id,
                    AvailabilityRule.weekday == on_date.weekday(),
                )
            )
        ).scalars().all()
        candidates = [
            (
                datetime.combine(on_date, r.start_time, tzinfo=tz),
                datetime.combine(on_date, r.end_time, tzinfo=tz),
                r.slot_duration_minutes,
            )
            for r in rules
        ]

    if not candidates:
        return []

    day_start = datetime.combine(on_date, datetime.min.time(), tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    booked = (
        await db.execute(
            select(Appointment.starts_at, Appointment.ends_at).where(
                Appointment.doctor_id == doctor.id,
                Appointment.status != AppointmentStatus.cancelled,
                Appointment.starts_at >= day_start,
                Appointment.starts_at < day_end,
            )
        )
    ).all()
    taken = {row.starts_at for row in booked}

    slots: list[Slot] = []
    for start_local, end_local, minutes in candidates:
        step = timedelta(minutes=minutes)
        cursor = start_local
        while cursor + step <= end_local:
            if cursor >= now and cursor not in taken:
                slots.append(Slot(starts_at=cursor, ends_at=cursor + step))
            cursor += step

    slots.sort(key=lambda s: s.starts_at)
    return slots


async def next_open_slots(
    db: AsyncSession,
    *,
    business: Business,
    doctor: Doctor,
    count: int = 3,
    search_days: int = 14,
    now: datetime | None = None,
) -> list[Slot]:
    """
    The next few free slots starting today, looking ahead up to search_days.

    What both the voice agent and the WhatsApp flow actually call - "when's
    Dr. Mehta free" doesn't come with a date attached, it wants the nearest
    options. Bounded by search_days so a doctor with no hours configured at
    all fails fast instead of scanning forever.
    """
    tz = ZoneInfo(business.timezone)
    now = now or datetime.now(tz)
    found: list[Slot] = []

    for offset in range(search_days):
        if len(found) >= count:
            break
        day = (now + timedelta(days=offset)).date()
        found.extend(
            await open_slots(db, business=business, doctor=doctor, on_date=day, now=now)
        )

    return found[:count]
