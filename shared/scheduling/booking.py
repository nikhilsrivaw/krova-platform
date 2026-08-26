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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Appointment, AppointmentStatus, Customer, Doctor, IntakeChannel
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
