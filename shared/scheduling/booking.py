"""
Turning a free slot into a held one, and moving or releasing it again.

The pre-check (is this slot in open_slots()?) is a courtesy, not the
guarantee - two callers can both see the same free slot and both try to take
it, voice and WhatsApp included. The actual guarantee is the partial unique
index on (doctor_id, starts_at) from the scheduling engine migration: the
database rejects the second write, and this module turns that rejection into
a clear SlotUnavailable rather than a raw IntegrityError leaking upward.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    Appointment,
    AppointmentStatus,
    Business,
    Customer,
    Doctor,
    IntakeChannel,
    Property,
)
from shared import verticals
from shared.scheduling import availability
from shared.scheduling.availability import Slot
from shared.utils.logging import get_logger

logger = get_logger(__name__)


class SlotUnavailable(Exception):
    """The requested slot is no longer free - lost a race, or was never valid."""


async def book(
    db: AsyncSession,
    *,
    business_id: uuid.UUID,
    doctor: Doctor,
    customer: Customer,
    slot: Slot,
    intake_channel: IntakeChannel,
    source_message_ids: list[uuid.UUID] | None = None,
    notes: str | None = None,
    property_id: uuid.UUID | None = None,
) -> Appointment:
    """
    Hold a slot for a customer.

    source_message_ids is required for voice and whatsapp bookings - an
    AI-mediated action must cite the conversation that authorised it, the
    same rule Commitment and Insight already follow. Only manual (staff-
    entered) bookings may omit it.
    """
    if intake_channel != IntakeChannel.manual and not source_message_ids:
        raise ValueError(
            f"{intake_channel.value} bookings must cite source_message_ids"
        )

    # Captured before any flush attempt: if the insert conflicts, only the
    # savepoint below rolls back, not the caller's outer transaction - but
    # ORM attributes can still end up expired, and touching an expired
    # attribute outside an awaited call is what caused the MissingGreenlet
    # bug this replaced. Plain locals sidestep the whole question.
    doctor_id, slot_start = doctor.id, slot.starts_at

    appointment = Appointment(
        business_id=business_id,
        doctor_id=doctor_id,
        customer_id=customer.id,
        property_id=property_id,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        status=AppointmentStatus.confirmed,
        intake_channel=intake_channel,
        source_message_ids=source_message_ids,
        notes=notes,
    )
    try:
        async with db.begin_nested():
            db.add(appointment)
            await db.flush()
    except IntegrityError as exc:
        logger.info(
            "slot already taken doctor=%s starts_at=%s",
            doctor_id, slot_start.isoformat(),
        )
        raise SlotUnavailable(
            f"{slot_start.isoformat()} was just taken - offer the next slot"
        ) from exc

    logger.info(
        "appointment booked id=%s doctor=%s channel=%s",
        appointment.id, doctor_id, intake_channel.value,
    )
    return appointment


async def try_book_from_agent(
    db: AsyncSession,
    *,
    book_slot: str | None,
    book_doctor: str | None,
    book_property: str | None,
    business: Business,
    customer: Customer,
    intake_channel: IntakeChannel,
    source_message_ids: list[uuid.UUID],
) -> Appointment | None:
    """
    Turn an agent's booking decision (book_slot/book_doctor/book_property,
    from shared/ai/agent.py's REPLY_TOOL schema or SYSTEM_STREAM's plain-text
    equivalent) into a real Appointment, or None for every way this can
    legitimately fail: an unparseable time, a doctor/property name that
    doesn't match, or the slot having been taken in the gap between the
    model reading its context and this running.

    Shared between the text-channel draft flow and a live call's stream_reply
    - originally only respond.py's own _try_book, since drafts had a message
    to hang the decision on. A call's booking decision has the exact same
    shape and the exact same failure modes, so it earns the same function
    rather than a second, drifting copy of this matching logic.

    Returns None rather than raising for anything short of "the slot itself
    was just taken" (SlotUnavailable, from book() below) - the caller's job
    in every case is the same: do not confirm a booking that did not happen,
    never surface an unparseable name or race as if it were the agent's own
    decision to escalate instead.

    Gated on the scheduling capability here, in the one shared function,
    rather than in each of respond.py's and pipeline.py's call sites -
    shared/ai/agent.py's REPLY_TOOL always includes book_slot regardless of
    vertical, so without this a non-scheduling business (a restaurant, a
    law firm) could get a real Appointment row from a hallucinated slot the
    model had no real availability data to build from.
    """
    if not book_slot:
        return None

    if not verticals.has_capability(business.vertical, "scheduling"):
        logger.warning(
            "agent returned book_slot for a non-scheduling business=%s, ignoring",
            business.id,
        )
        return None

    try:
        requested = datetime.fromisoformat(book_slot)
    except ValueError:
        logger.warning("agent returned unparseable book_slot %r", book_slot)
        return None

    doctors = (
        await db.execute(
            select(Doctor).where(Doctor.business_id == business.id, Doctor.active == True)  # noqa: E712
        )
    ).scalars().all()
    if not doctors:
        return None
    doctor = doctors[0]
    if len(doctors) > 1:
        if not book_doctor:
            logger.warning("book_slot set with no book_doctor across %d doctors", len(doctors))
            return None
        wanted = book_doctor.strip().lower()
        matches = [d for d in doctors if d.name.strip().lower() == wanted]
        if not matches:
            logger.warning("book_doctor %r matched no active doctor", book_doctor)
            return None
        doctor = matches[0]

    # Only resolved when the model actually named one - most verticals with
    # scheduling have no property_listings capability at all, and book_property
    # is correctly absent for every one of them. A name that fails to match
    # a real, active listing aborts the booking rather than proceeding
    # unlinked - the same "an unmatched name means do not trust the rest of
    # this either" rule book_doctor already follows.
    property_id: uuid.UUID | None = None
    if book_property:
        properties = (
            await db.execute(
                select(Property).where(
                    Property.business_id == business.id, Property.active == True  # noqa: E712
                )
            )
        ).scalars().all()
        wanted_property = book_property.strip().lower()
        property_matches = [p for p in properties if p.title.strip().lower() == wanted_property]
        if not property_matches:
            logger.warning("book_property %r matched no active listing", book_property)
            return None
        property_id = property_matches[0].id

    # open_slots() is re-run here, not trusted from whenever the context was
    # built - it is the single source of truth for "still free", and this
    # doubles as the check that the model did not invent a time.
    same_day = await availability.open_slots(
        db, business=business, doctor=doctor, on_date=requested.date()
    )
    slot = next((s for s in same_day if s.starts_at == requested), None)
    if slot is None:
        logger.info("book_slot %s no longer open for doctor=%s", book_slot, doctor.id)
        return None

    try:
        return await book(
            db,
            business_id=business.id,
            doctor=doctor,
            customer=customer,
            slot=slot,
            intake_channel=intake_channel,
            source_message_ids=source_message_ids,
            property_id=property_id,
        )
    except SlotUnavailable:
        return None


async def cancel(db: AsyncSession, *, appointment: Appointment, reason: str | None = None) -> Appointment:
    """Release a slot. The row stays - staff can still see it was booked and cancelled."""
    appointment.status = AppointmentStatus.cancelled
    if reason:
        appointment.notes = f"{appointment.notes}\nCancelled: {reason}" if appointment.notes else f"Cancelled: {reason}"
    await db.flush()
    logger.info("appointment cancelled id=%s", appointment.id)
    return appointment


async def reschedule(
    db: AsyncSession,
    *,
    appointment: Appointment,
    new_slot: Slot,
    source_message_ids: list[uuid.UUID] | None = None,
) -> Appointment:
    """
    Move an existing appointment to a new slot in place, rather than
    cancel-and-rebook - it stays the same appointment to staff, with its
    history intact, not a cancelled row plus an unrelated new one.
    """
    appointment_id, previous_start = appointment.id, appointment.starts_at

    try:
        async with db.begin_nested():
            appointment.starts_at = new_slot.starts_at
            appointment.ends_at = new_slot.ends_at
            if source_message_ids:
                appointment.source_message_ids = source_message_ids
            # Rescheduling un-marks any reminder already sent for the old
            # time - the reminder worker must send fresh ones for the new slot.
            appointment.reminder_24h_sent_at = None
            appointment.reminder_2h_sent_at = None
            await db.flush()
    except IntegrityError as exc:
        logger.info(
            "reschedule slot taken id=%s attempted_start=%s",
            appointment_id, new_slot.starts_at.isoformat(),
        )
        raise SlotUnavailable(
            f"{new_slot.starts_at.isoformat()} was just taken - offer the next slot"
        ) from exc

    logger.info(
        "appointment rescheduled id=%s from=%s to=%s",
        appointment_id, previous_start.isoformat(), new_slot.starts_at.isoformat(),
    )
    return appointment
