"""
The door a clinic owner actually walks through to use the Scheduling
capability: add doctors, set their weekly hours, see the appointment book.

Everything shared/scheduling/* does - availability, booking, reminders -
assumes this data already exists. Without this router nothing upstream is
reachable by a real business, only by test scripts.
"""

import uuid
from datetime import date, datetime, time

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import Appointment, AvailabilityRule, Doctor
from shared.scheduling import availability as scheduling_availability
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/scheduling", tags=["scheduling"])


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
    rows = await db.execute(select(Doctor).where(Doctor.business_id == current_user.business))
    return [_doctor_out(d) for d in rows.scalars().all()]


@router.post("/doctors", response_model=DoctorOut, status_code=status.HTTP_201_CREATED)
async def create_doctor(body: DoctorIn, current_user: CurrentUserDep, db: DbDep) -> DoctorOut:
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
async def update_doctor(doctor_id: uuid.UUID, body: DoctorPatch, current_user: CurrentUserDep, db: DbDep) -> DoctorOut:
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
async def delete_doctor(doctor_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
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
    doctor_id: uuid.UUID, body: AvailabilityRuleIn, current_user: CurrentUserDep, db: DbDep
) -> AvailabilityRuleOut:
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
async def delete_rule(rule_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
    rule = await db.get(AvailabilityRule, rule_id)
    if rule is None or rule.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Availability rule not found")
    await db.delete(rule)
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
    from shared.db.models import Business

    business = await db.get(Business, current_user.business)
    doctor = await _owned_doctor(doctor_id, current_user.business, db)
    slots = await scheduling_availability.open_slots(db, business=business, doctor=doctor, on_date=on)
    return [SlotOut(starts_at=s.starts_at, ends_at=s.ends_at) for s in slots]


class AppointmentOut(BaseModel):
    id: str
    doctor_id: str
    doctor_name: str
    customer_id: str
    starts_at: datetime
    ends_at: datetime
    status: str
    intake_channel: str
    notes: str | None


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
    query = (
        select(Appointment, Doctor.name)
        .join(Doctor, Doctor.id == Appointment.doctor_id)
        .where(Appointment.business_id == current_user.business)
        .order_by(Appointment.starts_at)
    )
    if from_date:
        query = query.where(Appointment.starts_at >= from_date)
    if to_date:
        query = query.where(Appointment.starts_at < to_date)

    rows = await db.execute(query)
    return [
        AppointmentOut(
            id=str(a.id), doctor_id=str(a.doctor_id), doctor_name=doctor_name,
            customer_id=str(a.customer_id), starts_at=a.starts_at, ends_at=a.ends_at,
            status=a.status.value if hasattr(a.status, "value") else str(a.status),
            intake_channel=a.intake_channel.value if hasattr(a.intake_channel, "value") else str(a.intake_channel),
            notes=a.notes,
        )
        for a, doctor_name in rows.all()
    ]
