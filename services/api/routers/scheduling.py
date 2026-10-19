"""
The door a clinic owner actually walks through to use the Scheduling
capability: add doctors, set their weekly hours, see the appointment book.

Everything shared/scheduling/* does - availability, booking, reminders -
assumes this data already exists. Without this router nothing upstream is
reachable by a real business, only by test scripts.
"""

import uuid
from datetime import date, datetime, time, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep, OwnerOrAdminDep
from shared import verticals
from shared.db.models import (
    Appointment, AppointmentStatus, AvailabilityException, AvailabilityRule, Business, Customer, Doctor,
    IntakeChannel,
)
from shared.verticals import labels
from shared.scheduling import availability as scheduling_availability
from shared.scheduling import booking as scheduling_booking
from shared.scheduling.booking import SlotUnavailable
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/scheduling", tags=["scheduling"])


async def _require_scheduling(business_id: uuid.UUID, db: DbDep) -> Business:
    """
    Every endpoint here needs this - like Claims, there is no single shared
    "book a slot" choke point to gate once at the shared/scheduling layer
    (try_book_from_agent already gates itself, but that's the AI path only -
    every one of this router's 10 endpoints is a separate direct entry
    point). The sidebar hides /scheduling for a business without the
    capability, but that was the only boundary before this.
    """
    business = await db.get(Business, business_id)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    if not verticals.has_capability(business, "scheduling"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This business does not have the scheduling capability"
        )
    return business


class SchedulingLabelsOut(BaseModel):
    provider: str
    provider_plural: str
    credential_label: str
    fee_label: str
    booking_noun: str
    booking_noun_plural: str


@router.get("/labels", response_model=SchedulingLabelsOut)
async def get_scheduling_labels(current_user: CurrentUserDep, db: DbDep) -> SchedulingLabelsOut:
    """
    What this business calls a provider, a booking, and the rest -
    resolved across code defaults, its vertical's template, and its own
    settings. See shared/verticals/labels.py::scheduling_labels.
    """
    business = await _require_scheduling(current_user.business, db)
    return SchedulingLabelsOut(**labels.scheduling_labels(business))


# ── Doctors ──────────────────────────────────────────────────────────────

class DoctorIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    qualifications: str | None = Field(default=None, max_length=255)
    consultation_fee_paise: int | None = Field(default=None, ge=0)
    department_id: str | None = None


class DoctorPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    qualifications: str | None = None
    consultation_fee_paise: int | None = Field(default=None, ge=0)
    active: bool | None = None


class DoctorOut(BaseModel):
    id: str
    name: str
    qualifications: str | None
    consultation_fee_paise: int | None
    department_id: str | None
    active: bool


def _doctor_out(d: Doctor) -> DoctorOut:
    return DoctorOut(
        id=str(d.id), name=d.name, qualifications=d.qualifications,
        consultation_fee_paise=d.consultation_fee_paise,
        department_id=str(d.department_id) if d.department_id else None,
        active=d.active,
    )


@router.get("/doctors", response_model=list[DoctorOut])
async def list_doctors(current_user: CurrentUserDep, db: DbDep) -> list[DoctorOut]:
    await _require_scheduling(current_user.business, db)
    rows = await db.execute(select(Doctor).where(Doctor.business_id == current_user.business))
    return [_doctor_out(d) for d in rows.scalars().all()]


@router.post("/doctors", response_model=DoctorOut, status_code=status.HTTP_201_CREATED)
async def create_doctor(body: DoctorIn, current_user: OwnerOrAdminDep, db: DbDep) -> DoctorOut:
    await _require_scheduling(current_user.business, db)
    doctor = Doctor(
        business_id=current_user.business,
        name=body.name,
        qualifications=body.qualifications,
        consultation_fee_paise=body.consultation_fee_paise,
        department_id=uuid.UUID(body.department_id) if body.department_id else None,
    )
    db.add(doctor)
    await db.flush()
    logger.info("doctor created id=%s business=%s", doctor.id, current_user.business)
    return _doctor_out(doctor)


@router.patch("/doctors/{doctor_id}", response_model=DoctorOut)
async def update_doctor(doctor_id: uuid.UUID, body: DoctorPatch, current_user: OwnerOrAdminDep, db: DbDep) -> DoctorOut:
    await _require_scheduling(current_user.business, db)
    doctor = await db.get(Doctor, doctor_id)
    if doctor is None or doctor.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Doctor not found")
    for field in ("name", "qualifications", "consultation_fee_paise", "active"):
        value = getattr(body, field)
        if value is not None:
            setattr(doctor, field, value)
    await db.flush()
    return _doctor_out(doctor)


@router.delete("/doctors/{doctor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_doctor(doctor_id: uuid.UUID, current_user: OwnerOrAdminDep, db: DbDep) -> None:
    await _require_scheduling(current_user.business, db)
    doctor = await db.get(Doctor, doctor_id)
    if doctor is None or doctor.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Doctor not found")
    # Deactivate rather than delete: past appointments still reference this
    # doctor, and a hard delete would either orphan them or cascade-destroy
    # real booking history for one scheduling mistake.
    doctor.active = False
    await db.flush()


# ── Weekly availability ─────────────────────────────────────────────────

class AvailabilityRuleIn(BaseModel):
    weekday: int = Field(ge=0, le=6, description="Monday=0 .. Sunday=6")
    start_time: time
    end_time: time
    slot_duration_minutes: int = Field(default=15, gt=0)


class AvailabilityRuleOut(BaseModel):
    id: str
    weekday: int
    start_time: time
    end_time: time
    slot_duration_minutes: int


async def _owned_doctor(doctor_id: uuid.UUID, business_id: uuid.UUID, db: DbDep) -> Doctor:
    doctor = await db.get(Doctor, doctor_id)
    if doctor is None or doctor.business_id != business_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Doctor not found")
    return doctor


@router.get("/doctors/{doctor_id}/availability-rules", response_model=list[AvailabilityRuleOut])
async def list_rules(doctor_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> list[AvailabilityRuleOut]:
    await _require_scheduling(current_user.business, db)
    await _owned_doctor(doctor_id, current_user.business, db)
    rows = await db.execute(select(AvailabilityRule).where(AvailabilityRule.doctor_id == doctor_id))
    return [
        AvailabilityRuleOut(
            id=str(r.id), weekday=r.weekday, start_time=r.start_time,
            end_time=r.end_time, slot_duration_minutes=r.slot_duration_minutes,
        )
        for r in rows.scalars().all()
    ]


@router.post(
    "/doctors/{doctor_id}/availability-rules",
    response_model=AvailabilityRuleOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_rule(
    doctor_id: uuid.UUID, body: AvailabilityRuleIn, current_user: OwnerOrAdminDep, db: DbDep
) -> AvailabilityRuleOut:
    await _require_scheduling(current_user.business, db)
    if body.end_time <= body.start_time:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "end_time must be after start_time")
    doctor = await _owned_doctor(doctor_id, current_user.business, db)
    rule = AvailabilityRule(
        business_id=current_user.business, doctor_id=doctor.id,
        weekday=body.weekday, start_time=body.start_time, end_time=body.end_time,
        slot_duration_minutes=body.slot_duration_minutes,
    )
    db.add(rule)
    await db.flush()
    return AvailabilityRuleOut(
        id=str(rule.id), weekday=rule.weekday, start_time=rule.start_time,
        end_time=rule.end_time, slot_duration_minutes=rule.slot_duration_minutes,
    )


@router.delete("/availability-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: uuid.UUID, current_user: OwnerOrAdminDep, db: DbDep) -> None:
    await _require_scheduling(current_user.business, db)
    rule = await db.get(AvailabilityRule, rule_id)
    if rule is None or rule.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Availability rule not found")
    await db.delete(rule)
    await db.flush()


# ── One-off exceptions to the weekly pattern ────────────────────────────
# A day a provider is out (leave, a conference), or exceptionally in
# (an extra Saturday). shared/scheduling/availability.py already reads
# these rows on every slot computation - this is the only way to write one.

class AvailabilityExceptionIn(BaseModel):
    date: date
    is_unavailable: bool = True
    start_time: time | None = None
    end_time: time | None = None
    reason: str | None = Field(default=None, max_length=255)


class AffectedBooking(BaseModel):
    id: str
    customer_name: str | None
    starts_at: datetime


class AvailabilityExceptionOut(BaseModel):
    id: str
    date: date
    is_unavailable: bool
    start_time: time | None
    end_time: time | None
    reason: str | None
    # bookings already made for the time this takes away - shown so staff can tell or cancel them
    affected: list[AffectedBooking] = []


def _exception_out(e: AvailabilityException) -> AvailabilityExceptionOut:
    return AvailabilityExceptionOut(
        id=str(e.id), date=e.date, is_unavailable=e.is_unavailable,
        start_time=e.start_time, end_time=e.end_time, reason=e.reason,
    )


@router.get("/doctors/{doctor_id}/availability-exceptions", response_model=list[AvailabilityExceptionOut])
async def list_exceptions(
    doctor_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep
) -> list[AvailabilityExceptionOut]:
    await _require_scheduling(current_user.business, db)
    await _owned_doctor(doctor_id, current_user.business, db)
    rows = await db.execute(
        select(AvailabilityException)
        .where(AvailabilityException.doctor_id == doctor_id)
        .order_by(AvailabilityException.date)
    )
    return [_exception_out(e) for e in rows.scalars().all()]


@router.post(
    "/doctors/{doctor_id}/availability-exceptions",
    response_model=AvailabilityExceptionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_exception(
    doctor_id: uuid.UUID, body: AvailabilityExceptionIn, current_user: CurrentUserDep, db: DbDep
) -> AvailabilityExceptionOut:
    await _require_scheduling(current_user.business, db)
    if body.start_time is not None and body.end_time is not None and body.end_time <= body.start_time:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "end_time must be after start_time")
    doctor = await _owned_doctor(doctor_id, current_user.business, db)
    exception = AvailabilityException(
        business_id=current_user.business, doctor_id=doctor.id,
        date=body.date, is_unavailable=body.is_unavailable,
        start_time=body.start_time, end_time=body.end_time, reason=body.reason,
    )
    db.add(exception)
    await db.flush()
    out = _exception_out(exception)
    business = await db.get(Business, current_user.business)
    out.affected = [
        AffectedBooking(id=str(a.id), customer_name=name, starts_at=a.starts_at)
        for a, name in await _bookings_hit_by(db, exception, business.timezone if business else "Asia/Kolkata")
    ]
    return out


_LIVE_BOOKING = (AppointmentStatus.confirmed, AppointmentStatus.requested, AppointmentStatus.awaiting_deposit)


async def _bookings_hit_by(db, exception: AvailabilityException, tz_name: str) -> list:
    """Live bookings with this doctor during the time a day off (or changed hours) takes away, with the customer's name."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name or "Asia/Kolkata")
    day_start = datetime.combine(exception.date, time.min, tzinfo=tz)
    day_end = datetime.combine(exception.date, time.max, tzinfo=tz)
    rows = (
        await db.execute(
            select(Appointment, Customer.display_name)
            .join(Customer, Customer.id == Appointment.customer_id)
            .where(
                Appointment.doctor_id == exception.doctor_id, Appointment.status.in_(_LIVE_BOOKING),
                Appointment.starts_at >= day_start, Appointment.starts_at <= day_end,
            )
            .order_by(Appointment.starts_at)
        )
    ).all()
    hit = []
    for appointment, name in rows:
        local = appointment.starts_at.astimezone(tz).time()
        if exception.is_unavailable and (exception.start_time is None or exception.end_time is None):
            hit.append((appointment, name))  # the whole day is off
        elif exception.is_unavailable and exception.start_time <= local < exception.end_time:
            hit.append((appointment, name))  # inside the hours taken off
        elif not exception.is_unavailable and exception.start_time and exception.end_time and not (
            exception.start_time <= local < exception.end_time
        ):
            hit.append((appointment, name))  # outside the only hours left that day
    return hit


class CancelAffectedOut(BaseModel):
    cancelled: int


@router.post("/availability-exceptions/{exception_id}/cancel-bookings", response_model=CancelAffectedOut)
async def cancel_bookings_hit_by(exception_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> CancelAffectedOut:
    """
    Cancel the bookings a day off takes away. Each goes through the normal cancel, so its slot frees, the calendar
    updates and the business's own "appointment cancelled" automations run (that is where a message to the patient
    belongs - the business writes it).
    """
    business = await _require_scheduling(current_user.business, db)
    exception = await db.get(AvailabilityException, exception_id)
    if exception is None or exception.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Availability exception not found")
    reason = f"Not available on {exception.date.strftime('%d %b')}" + (f" - {exception.reason}" if exception.reason else "")
    count = 0
    for appointment, _ in await _bookings_hit_by(db, exception, business.timezone):
        await scheduling_booking.cancel(db, appointment=appointment, reason=reason)
        count += 1
    await db.flush()
    return CancelAffectedOut(cancelled=count)


@router.delete("/availability-exceptions/{exception_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_exception(exception_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
    await _require_scheduling(current_user.business, db)
    exception = await db.get(AvailabilityException, exception_id)
    if exception is None or exception.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Availability exception not found")
    await db.delete(exception)
    await db.flush()


# ── Reading the calendar ─────────────────────────────────────────────────

class SlotOut(BaseModel):
    starts_at: datetime
    ends_at: datetime


@router.get("/doctors/{doctor_id}/open-slots", response_model=list[SlotOut])
async def get_open_slots(
    doctor_id: uuid.UUID, on: date, current_user: CurrentUserDep, db: DbDep
) -> list[SlotOut]:
    """What a WhatsApp or voice conversation would see, exposed for the dashboard too."""
    business = await _require_scheduling(current_user.business, db)
    doctor = await _owned_doctor(doctor_id, current_user.business, db)
    slots = await scheduling_availability.open_slots(db, business=business, doctor=doctor, on_date=on)
    return [SlotOut(starts_at=s.starts_at, ends_at=s.ends_at) for s in slots]


class AppointmentOut(BaseModel):
    id: str
    doctor_id: str
    doctor_name: str
    customer_id: str
    property_id: str | None
    starts_at: datetime
    ends_at: datetime
    status: str
    intake_channel: str
    notes: str | None
    # Filled on the list, so a screen can show who without fetching every customer.
    customer_name: str | None = None


def _appointment_out(a: Appointment, doctor_name: str, customer_name: str | None = None) -> AppointmentOut:
    return AppointmentOut(
        id=str(a.id), doctor_id=str(a.doctor_id), doctor_name=doctor_name,
        customer_id=str(a.customer_id),
        property_id=str(a.property_id) if a.property_id else None,
        starts_at=a.starts_at, ends_at=a.ends_at,
        status=a.status.value if hasattr(a.status, "value") else str(a.status),
        intake_channel=a.intake_channel.value if hasattr(a.intake_channel, "value") else str(a.intake_channel),
        notes=a.notes,
        customer_name=customer_name,
    )


@router.get("/appointments", response_model=list[AppointmentOut])
async def list_appointments(
    current_user: CurrentUserDep,
    db: DbDep,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
) -> list[AppointmentOut]:
    """
    One calendar, whichever channel the booking came from - intake_channel
    is always in the response, never hidden from staff.
    """
    await _require_scheduling(current_user.business, db)
    query = (
        select(Appointment, Doctor.name, Customer.display_name)
        .join(Doctor, Doctor.id == Appointment.doctor_id)
        .outerjoin(Customer, Customer.id == Appointment.customer_id)
        .where(Appointment.business_id == current_user.business)
        .order_by(Appointment.starts_at)
    )
    if from_date:
        query = query.where(Appointment.starts_at >= from_date)
    if to_date:
        query = query.where(Appointment.starts_at < to_date)

    rows = await db.execute(query)
    return [_appointment_out(a, doctor_name, customer_name) for a, doctor_name, customer_name in rows.all()]


class AppointmentIn(BaseModel):
    doctor_id: str
    customer_id: str
    starts_at: datetime
    # Only meaningful for a business with property_listings - which
    # property this viewing is for. Ignored (left null) otherwise.
    property_id: str | None = None
    notes: str | None = None


@router.post("/appointments", response_model=AppointmentOut, status_code=status.HTTP_201_CREATED)
async def create_appointment(
    body: AppointmentIn, current_user: CurrentUserDep, db: DbDep
) -> AppointmentOut:
    """
    Staff booking a slot directly - a phone call that never went through the
    voice agent, a walk-in, a viewing arranged in person. The one thing this
    must never skip even for a human doing the booking: the requested time
    still has to be a real open slot, the same check WhatsApp and voice
    booking both go through - a front desk fat-fingering a time is exactly
    the kind of double-booking this whole engine exists to prevent.
    """
    business = await _require_scheduling(current_user.business, db)
    doctor = await _owned_doctor(uuid.UUID(body.doctor_id), current_user.business, db)
    customer = await db.get(Customer, uuid.UUID(body.customer_id))
    if customer is None or customer.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")

    day_slots = await scheduling_availability.open_slots(
        db, business=business, doctor=doctor, on_date=body.starts_at.date()
    )
    slot = next((s for s in day_slots if s.starts_at == body.starts_at), None)
    if slot is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{body.starts_at.isoformat()} is not an open slot for this doctor",
        )

    try:
        appointment = await scheduling_booking.book(
            db,
            business_id=current_user.business,
            doctor=doctor,
            customer=customer,
            slot=slot,
            intake_channel=IntakeChannel.manual,
            notes=body.notes,
            property_id=uuid.UUID(body.property_id) if body.property_id else None,
        )
    except SlotUnavailable:
        raise HTTPException(status.HTTP_409_CONFLICT, "That slot was just taken")

    return _appointment_out(appointment, doctor.name)


async def _owned_appointment(appointment_id: uuid.UUID, business_id: uuid.UUID, db: DbDep) -> Appointment:
    appointment = await db.get(Appointment, appointment_id)
    if appointment is None or appointment.business_id != business_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found")
    return appointment


class AppointmentCancelIn(BaseModel):
    reason: str | None = None


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentOut)
async def cancel_appointment(
    appointment_id: uuid.UUID, body: AppointmentCancelIn, current_user: CurrentUserDep, db: DbDep
) -> AppointmentOut:
    await _require_scheduling(current_user.business, db)
    appointment = await _owned_appointment(appointment_id, current_user.business, db)
    doctor = await db.get(Doctor, appointment.doctor_id)
    appointment = await scheduling_booking.cancel(db, appointment=appointment, reason=body.reason)
    return _appointment_out(appointment, doctor.name if doctor else "")


class AppointmentStatusIn(BaseModel):
    status: str  # confirmed | visited | no_show


_SETTABLE = {"confirmed", "visited", "no_show"}
# Held, waiting on a deposit, or cancelled: not the staff's to flip to "came" or "did not come".
_LOCKED = {AppointmentStatus.cancelled, AppointmentStatus.awaiting_deposit}


def next_status(current: AppointmentStatus, wanted: str, starts_at: datetime, now: datetime) -> AppointmentStatus:
    """
    What staff may record after the fact: the person came, did not come, or (to undo a
    slip) is simply booked again. Raises ValueError with a message fit to show.
    """
    if wanted not in _SETTABLE:
        raise ValueError("status must be confirmed, visited or no_show")
    if current in _LOCKED:
        raise ValueError(f"A {current.value.replace('_', ' ')} booking cannot be marked this way")
    if wanted == "no_show" and starts_at > now:
        raise ValueError("A booking that has not started yet cannot be marked as a no-show")
    return AppointmentStatus(wanted)


@router.post("/appointments/{appointment_id}/status", response_model=AppointmentOut)
async def set_appointment_status(
    appointment_id: uuid.UUID, body: AppointmentStatusIn, current_user: CurrentUserDep, db: DbDep
) -> AppointmentOut:
    """Mark a booking as visited / no-show (or back to confirmed). This is what recall and no-show history read."""
    await _require_scheduling(current_user.business, db)
    appointment = await _owned_appointment(appointment_id, current_user.business, db)
    before = appointment.status
    try:
        appointment.status = next_status(appointment.status, body.status, appointment.starts_at, datetime.now(timezone.utc))
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    doctor = await db.get(Doctor, appointment.doctor_id)
    if appointment.status != before and appointment.status in (AppointmentStatus.visited, AppointmentStatus.no_show):
        await db.flush()
        await _fire_outcome(db, appointment, doctor.name if doctor else "")
    await db.commit()
    return _appointment_out(appointment, doctor.name if doctor else "")


async def _fire_outcome(db, appointment: Appointment, provider: str) -> None:
    """appointment.visited / appointment.no_show, with how many of each this customer now has."""
    from sqlalchemy import func

    from shared.care import post_call_actions

    count = (await db.execute(
        select(func.count(Appointment.id)).where(
            Appointment.business_id == appointment.business_id, Appointment.customer_id == appointment.customer_id,
            Appointment.status == appointment.status,
        )
    )).scalar_one()
    visited = appointment.status == AppointmentStatus.visited
    channel = appointment.intake_channel.value if hasattr(appointment.intake_channel, "value") else None
    context = {"starts_at": appointment.starts_at.isoformat(), "intake_channel": channel, "provider": provider}
    context["visit_number" if visited else "no_show_count"] = count
    await post_call_actions.fire_safely(
        db, business_id=appointment.business_id,
        trigger_type="appointment.visited" if visited else "appointment.no_show",
        customer_id=appointment.customer_id, channel=channel, context=context,
    )


class AppointmentRescheduleIn(BaseModel):
    starts_at: datetime


@router.post("/appointments/{appointment_id}/reschedule", response_model=AppointmentOut)
async def reschedule_appointment(
    appointment_id: uuid.UUID, body: AppointmentRescheduleIn, current_user: CurrentUserDep, db: DbDep
) -> AppointmentOut:
    """Same real-slot check create_appointment makes - a new time must
    actually be open, never trusted from the request alone."""
    business = await _require_scheduling(current_user.business, db)
    appointment = await _owned_appointment(appointment_id, current_user.business, db)
    doctor = await _owned_doctor(appointment.doctor_id, current_user.business, db)

    day_slots = await scheduling_availability.open_slots(
        db, business=business, doctor=doctor, on_date=body.starts_at.date()
    )
    slot = next((s for s in day_slots if s.starts_at == body.starts_at), None)
    if slot is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{body.starts_at.isoformat()} is not an open slot for this doctor",
        )

    try:
        appointment = await scheduling_booking.reschedule(db, appointment=appointment, new_slot=slot)
    except SlotUnavailable:
        raise HTTPException(status.HTTP_409_CONFLICT, "That slot was just taken")

    return _appointment_out(appointment, doctor.name)
