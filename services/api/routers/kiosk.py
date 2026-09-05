"""
The public self-service check-in kiosk - no login, meant for a tablet
sitting at a clinic's front desk. A walk-in patient types their own name
and phone, taps a shift, and gets a token - the same shared/scheduling/
queue_booking.py logic staff check-in and the voice/WhatsApp agent use.

Auth model: this codebase's own established pattern for "public but not
just anyone" (see services/api/routers/webhooks.py's Shopify handler) is
resolve-the-tenant-from-a-public-identifier. Business.kiosk_token is that
identifier here - opaque, rotatable, generated only when a clinic turns
kiosk check-in on. An unknown or null token is dropped as 404, not treated
as a leaky "invalid" error that would help someone guess a real one.

Deliberately no CurrentUserDep anywhere in this file - DbDep only.
"""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import DbDep
from shared.db.models import Business, IdentityKind, IntakeChannel, QueueEntry, QueueStatus, Shift
from shared.identity import resolver
from shared.scheduling import queue_booking
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/kiosk", tags=["kiosk"])


async def _business_for_token(token: str, db: DbDep) -> Business:
    result = await db.execute(select(Business).where(Business.kiosk_token == token, Business.is_active.is_(True)))
    business = result.scalars().first()
    if business is None:
        # Same "unknown identifier, nothing happens" shape webhooks.py uses -
        # never confirm or deny whether a token almost-matched something.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Kiosk not found")
    return business


class OpenShiftOut(BaseModel):
    shift: str
    waiting_count: int


class KioskStatusOut(BaseModel):
    business_name: str
    open_shifts: list[OpenShiftOut]


@router.get("/{token}/status", response_model=KioskStatusOut)
async def kiosk_status(token: str, db: DbDep) -> KioskStatusOut:
    business = await _business_for_token(token, db)
    summary = await queue_booking.open_shift_summary(db, business_id=business.id)
    open_shifts = [OpenShiftOut(shift=shift.value, waiting_count=count) for shift, count in summary]
    return KioskStatusOut(business_name=business.name, open_shifts=open_shifts)


class KioskCheckInIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    phone: str = Field(min_length=6, max_length=20)
    shift: Shift


class KioskCheckInOut(BaseModel):
    queue_number: int
    shift: str
    ahead_of_you: int


@router.post("/{token}/check-in", response_model=KioskCheckInOut, status_code=status.HTTP_201_CREATED)
async def kiosk_check_in(token: str, body: KioskCheckInIn, db: DbDep) -> KioskCheckInOut:
    business = await _business_for_token(token, db)

    resolution = await resolver.resolve(
        business.id, IdentityKind.phone, body.phone, db, display_name=body.name,
    )

    try:
        entry = await queue_booking.issue_token(
            db,
            business_id=business.id,
            shift=body.shift,
            customer_id=resolution.customer.id,
            doctor_id=None,
            intake_channel=IntakeChannel.manual,
        )
    except queue_booking.ShiftNotOpen as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    ahead = (
        await db.execute(
            select(QueueEntry).where(
                QueueEntry.business_id == business.id,
                QueueEntry.queue_date == entry.queue_date,
                QueueEntry.shift == entry.shift,
                QueueEntry.status == QueueStatus.waiting,
                QueueEntry.queue_number < entry.queue_number,
            )
        )
    ).scalars().all()

    return KioskCheckInOut(
        queue_number=entry.queue_number,
        shift=entry.shift.value,
        ahead_of_you=len(ahead),
    )
