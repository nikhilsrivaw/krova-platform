"""
The first and second appointment reminder sweep - 24h and 2h out by
default, business-configurable per Business.settings["scheduling"]
["reminder_windows"].

Research on this exact pattern (see the vertical-templates planning
conversation) showed WhatsApp reminders cut clinic no-shows 35-70% - the
single highest-leverage thing this capability does after booking itself.
The 24h/2h pair is right for a clinic's next-day visit; a real_estate
viewing booked weeks out plausibly wants a longer lead time, hence the
override.

A time-window sweep, not a scheduled-per-appointment job: simpler to reason
about, and naturally self-limiting - an appointment whose window passed
without a successful send is never retried forever, because the query stops
matching it once `now` moves past the window. Nothing else about it needs
a queue or a stalled-job story.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Appointment, AppointmentStatus, Business, Customer, Doctor
from shared.scheduling import notify
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Wide enough that a 15-minute poll cycle can never fully skip an
# appointment's window even if one run is late or misfires.
_WINDOW = timedelta(minutes=10)

_DEFAULT_FIRST_HOURS = 24.0
_DEFAULT_SECOND_HOURS = 2.0

# Not a discovered real value - a generous ceiling nobody would plausibly
# configure a reminder past (a "reminder" more than 2 days out stops being
# one). Needed because the DB query below can no longer filter on one
# global target the way it used to: each business may have its own window,
# so the query fetches every confirmed candidate in this range and the
# precise per-business check happens in Python once that business's row is
# in hand.
_MAX_LOOKAHEAD = timedelta(hours=48)


def _reminder_hours(business: Business) -> tuple[float, float]:
    raw = ((business.settings or {}).get("scheduling") or {}).get("reminder_windows") or {}
    first, second = raw.get("first_hours"), raw.get("second_hours")
    first = float(first) if isinstance(first, (int, float)) and first > 0 else _DEFAULT_FIRST_HOURS
    second = float(second) if isinstance(second, (int, float)) and second > 0 else _DEFAULT_SECOND_HOURS
    return first, second


async def _candidates(db: AsyncSession, *, already_sent) -> list[Appointment]:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Appointment).where(
            Appointment.status == AppointmentStatus.confirmed,
            already_sent.is_(None),
            Appointment.starts_at >= now,
            Appointment.starts_at <= now + _MAX_LOOKAHEAD,
        )
    )
    return list(result.scalars().all())


async def send_due_reminders(db: AsyncSession) -> int:
    """Send every first/second reminder currently due. Returns how many actually sent."""
    sent = 0
    now = datetime.now(timezone.utc)

    candidates_first = await _candidates(db, already_sent=Appointment.reminder_24h_sent_at)
    candidates_second = await _candidates(db, already_sent=Appointment.reminder_2h_sent_at)

    for appointment, field, which in [(a, "reminder_24h_sent_at", "first") for a in candidates_first] + [
        (a, "reminder_2h_sent_at", "second") for a in candidates_second
    ]:
        business = await db.get(Business, appointment.business_id)
        if business is None:
            continue
        first_hours, second_hours = _reminder_hours(business)
        target = appointment.starts_at - timedelta(hours=first_hours if which == "first" else second_hours)
        if not (now - _WINDOW <= target <= now + _WINDOW):
            continue

        doctor = await db.get(Doctor, appointment.doctor_id)
        customer = await db.get(Customer, appointment.customer_id)
        if doctor is None or customer is None:
            continue

        ok = await notify.send_reminder(
            db, business=business, customer=customer, doctor=doctor, starts_at=appointment.starts_at,
        )
        if ok:
            setattr(appointment, field, now)
            sent += 1

    return sent
