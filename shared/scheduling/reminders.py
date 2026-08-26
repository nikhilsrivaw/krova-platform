"""
The 24-hour and 2-hour appointment reminder sweep.

Research on this exact pattern (see the vertical-templates planning
conversation) showed WhatsApp reminders cut clinic no-shows 35-70% - the
single highest-leverage thing this capability does after booking itself.

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


async def _due(db: AsyncSession, *, target_in: timedelta, already_sent) -> list[Appointment]:
    now = datetime.now(timezone.utc)
    target = now + target_in
    result = await db.execute(
        select(Appointment).where(
            Appointment.status == AppointmentStatus.confirmed,
            already_sent.is_(None),
            Appointment.starts_at >= target - _WINDOW,
            Appointment.starts_at <= target + _WINDOW,
        )
    )
    return list(result.scalars().all())


async def send_due_reminders(db: AsyncSession) -> int:
    """Send every 24h and 2h reminder currently due. Returns how many actually sent."""
    sent = 0
    now = datetime.now(timezone.utc)

    due_24h = await _due(db, target_in=timedelta(hours=24), already_sent=Appointment.reminder_24h_sent_at)
    due_2h = await _due(db, target_in=timedelta(hours=2), already_sent=Appointment.reminder_2h_sent_at)

    for appointment, field in [(a, "reminder_24h_sent_at") for a in due_24h] + [
        (a, "reminder_2h_sent_at") for a in due_2h
    ]:
        business = await db.get(Business, appointment.business_id)
        doctor = await db.get(Doctor, appointment.doctor_id)
        customer = await db.get(Customer, appointment.customer_id)
        if business is None or doctor is None or customer is None:
            continue

        ok = await notify.send_reminder(
            db, business=business, customer=customer, doctor=doctor, starts_at=appointment.starts_at,
        )
        if ok:
            setattr(appointment, field, now)
            sent += 1

    return sent
