"""
Turning a "meeting" commitment extracted from a conversation into a real,
verified appointment - the missing half of what clinic.json already asks
commitment extraction to watch for.

Extraction only knows what was said; it has no idea whether that time is
real or already taken. This enforces the same safety boundary WhatsApp's
book_slot path already does (see services/workers/respond.py): a claimed
time is only ever written as an Appointment after re-checking it against
real, live availability. A commitment that cannot be verified this way is
left exactly as extraction left it - an open commitment for a human to
resolve - never silently promoted to a booking on trust.

This is what makes voice booking work without the streaming call ever
needing to call a tool mid-reply: the live turn just talks, naturally,
using the real availability already in its context (shared/ai/context.py);
this runs afterward, on the same per-message analysis pass every channel
already gets, and turns a confirmed time into an actual reservation.
"""

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared import verticals
from shared.db.models import Appointment, Business, CommitmentKind, Customer, Doctor, IntakeChannel
from shared.scheduling import availability as scheduling_availability
from shared.scheduling import booking as scheduling_booking
from shared.scheduling.booking import SlotUnavailable
from shared.utils.logging import get_logger

logger = get_logger(__name__)


async def try_book_from_commitment(
    db: AsyncSession,
    *,
    business_id: uuid.UUID,
    customer_id: uuid.UUID,
    kind: CommitmentKind,
    description: str,
    due_at: datetime | None,
    due_at_explicit: bool,
    source_message_ids: list[uuid.UUID],
) -> Appointment | None:
    """
    Attempt to turn one freshly-extracted "meeting" commitment into a real
    booking. Returns the Appointment if it happened, so a caller can send a
    confirmation - None otherwise.

    None is the common, correct outcome: for every business that has not
    declared the scheduling capability, and for any commitment this cannot
    confidently resolve to one real doctor and one real, still-open slot -
    an inferred date ("next week" -> due_at_explicit=False), an ambiguous
    doctor at a multi-doctor clinic, or a time that simply is not a real
    slot. Every one of those is left as a plain commitment, same as today.
    """
    if kind != CommitmentKind.meeting or due_at is None or not due_at_explicit:
        return None

    business = await db.get(Business, business_id)
    if business is None or not verticals.has_capability(business.vertical, "scheduling"):
        return None

    if due_at.tzinfo is None:
        # Extraction should always produce timezone-aware datetimes; this is
        # a defensive fallback, not the expected path.
        due_at = due_at.replace(tzinfo=ZoneInfo(business.timezone))

    doctors = (
        await db.execute(
            select(Doctor).where(Doctor.business_id == business_id, Doctor.active == True)  # noqa: E712
        )
    ).scalars().all()
    if not doctors:
        return None

    doctor = doctors[0]
    if len(doctors) > 1:
        named = [d for d in doctors if d.name.lower() in description.lower()]
        if len(named) != 1:
            logger.info(
                "meeting commitment for business=%s names no single doctor among %d active, leaving as manual",
                business_id, len(doctors),
            )
            return None
        doctor = named[0]

    local_date = due_at.astimezone(ZoneInfo(business.timezone)).date()
    same_day = await scheduling_availability.open_slots(db, business=business, doctor=doctor, on_date=local_date)
    slot = next((s for s in same_day if s.starts_at == due_at), None)
    if slot is None:
        logger.info(
            "commitment due_at=%s does not match a real open slot for doctor=%s, leaving as manual",
            due_at.isoformat(), doctor.id,
        )
        return None

    customer = await db.get(Customer, customer_id)
    if customer is None:
        return None

    try:
        return await scheduling_booking.book(
            db,
            business_id=business_id,
            doctor=doctor,
            customer=customer,
            slot=slot,
            intake_channel=IntakeChannel.voice,
            source_message_ids=source_message_ids,
            notes=description,
        )
    except SlotUnavailable:
        return None
