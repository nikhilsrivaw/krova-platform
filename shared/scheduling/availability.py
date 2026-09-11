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


def _day_slots_from_prefetched(
    *,
    on_date: date,
    tz: ZoneInfo,
    now: datetime,
    exception: AvailabilityException | None,
    weekday_rules: list[AvailabilityRule],
    usual_duration: int | None,
    taken: set[datetime],
) -> list[Slot]:
    """
    Same candidate-window/stepping logic as open_slots(), operating on
    rules/exceptions/appointments already fetched for the whole search
    window rather than querying per day - see next_open_slots()'s own
    docstring for why. Kept as its own copy rather than a helper
    open_slots() also calls, so open_slots() itself - used far more
    widely (booking's own pre-check, the public API, WhatsApp flows) -
    stays completely untouched.
    """
    if exception and exception.is_unavailable:
        return []

    if exception and not exception.is_unavailable and exception.start_time and exception.end_time:
        candidates: list[tuple[datetime, datetime, int]] = [(
            datetime.combine(on_date, exception.start_time, tzinfo=tz),
            datetime.combine(on_date, exception.end_time, tzinfo=tz),
            usual_duration or 15,
        )]
    else:
        candidates = [
            (
                datetime.combine(on_date, r.start_time, tzinfo=tz),
                datetime.combine(on_date, r.end_time, tzinfo=tz),
                r.slot_duration_minutes,
            )
            for r in weekday_rules
        ]

    if not candidates:
        return []

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

    Fetches the whole search window's rules/exceptions/appointments in
    three queries up front rather than open_slots()'s own per-day queries
    repeated up to search_days times over - this sits on the path a live
    caller waits through (shared/ai/context.py builds it before the agent
    can reply at all), and a doctor with no near-term availability used to
    mean up to search_days * 2-3 sequential round trips before this
    returned anything.
    """
    tz = ZoneInfo(business.timezone)
    now = now or datetime.now(tz)

    range_start_date = now.date()
    range_end_date = range_start_date + timedelta(days=search_days)
    range_start = datetime.combine(range_start_date, datetime.min.time(), tzinfo=tz)
    range_end = datetime.combine(range_end_date, datetime.min.time(), tzinfo=tz)

    rules = (
        await db.execute(
            select(AvailabilityRule).where(AvailabilityRule.doctor_id == doctor.id)
        )
    ).scalars().all()
    rules_by_weekday: dict[int, list[AvailabilityRule]] = {}
    for r in rules:
        rules_by_weekday.setdefault(r.weekday, []).append(r)
    # Same fallback open_slots() itself uses for an exception day with no
    # matching weekday rule - any one rule's duration, no ordering ever
    # guaranteed there either.
    usual_duration = rules[0].slot_duration_minutes if rules else None

    exceptions = (
        await db.execute(
            select(AvailabilityException).where(
                AvailabilityException.doctor_id == doctor.id,
                AvailabilityException.date >= range_start_date,
                AvailabilityException.date < range_end_date,
            )
        )
    ).scalars().all()
    exception_by_date = {e.date: e for e in exceptions}

    booked = (
        await db.execute(
            select(Appointment.starts_at).where(
                Appointment.doctor_id == doctor.id,
                Appointment.status != AppointmentStatus.cancelled,
                Appointment.starts_at >= range_start,
                Appointment.starts_at < range_end,
            )
        )
    ).all()
    taken_by_date: dict[date, set[datetime]] = {}
    for row in booked:
        taken_by_date.setdefault(row.starts_at.astimezone(tz).date(), set()).add(row.starts_at)

    found: list[Slot] = []
    for offset in range(search_days):
        if len(found) >= count:
            break
        on_date = (now + timedelta(days=offset)).date()
        found.extend(
            _day_slots_from_prefetched(
                on_date=on_date,
                tz=tz,
                now=now,
                exception=exception_by_date.get(on_date),
                weekday_rules=rules_by_weekday.get(on_date.weekday(), []),
                usual_duration=usual_duration,
                taken=taken_by_date.get(on_date, set()),
            )
        )

    return found[:count]
