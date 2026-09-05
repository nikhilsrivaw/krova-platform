"""
The OPD Queue capability's door: open/close today's shifts, check a patient
in, see today's live list, call the next one. See shared/db/models/queue.py
and shared/db/models/shift.py for why this is not the Scheduling capability
with different labels, and shared/scheduling/queue_booking.py for the
shared "issue a token" logic every entry point (this router, the public
kiosk, the voice/WhatsApp agent) goes through.
"""

import secrets
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import Business, Customer, IntakeChannel, QueueEntry, QueueStatus, Shift, ShiftSession
from shared.scheduling import queue_booking
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/queue", tags=["queue"])


# ── Shifts ───────────────────────────────────────────────────────────────

class ShiftSessionOut(BaseModel):
    id: str
    shift: str
    session_date: date
    opened_at: datetime
    closed_at: datetime | None


def _shift_out(s: ShiftSession) -> ShiftSessionOut:
    return ShiftSessionOut(
        id=str(s.id), shift=s.shift.value if hasattr(s.shift, "value") else str(s.shift),
        session_date=s.session_date, opened_at=s.opened_at, closed_at=s.closed_at,
    )


class OpenShiftIn(BaseModel):
    shift: Shift


@router.post("/shifts/open", response_model=ShiftSessionOut, status_code=status.HTTP_201_CREATED)
async def open_shift(body: OpenShiftIn, current_user: CurrentUserDep, db: DbDep) -> ShiftSessionOut:
    """Opens today's session for a shift, or reopens it if it was closed earlier today."""
    today = datetime.now(timezone.utc).date()
    existing = await queue_booking.get_open_session(
        db, business_id=current_user.business, shift=body.shift, on_date=today
    )
    if existing is not None:
        return _shift_out(existing)

    closed_today = (
        await db.execute(
            select(ShiftSession).where(
                ShiftSession.business_id == current_user.business,
                ShiftSession.shift == body.shift,
                ShiftSession.session_date == today,
            )
        )
    ).scalars().first()

    now = datetime.now(timezone.utc)
    if closed_today is not None:
        closed_today.closed_at = None
        closed_today.opened_at = now
        closed_today.opened_by_user_id = current_user.id
        await db.flush()
        logger.info("shift reopened id=%s business=%s shift=%s", closed_today.id, current_user.business, body.shift.value)
        return _shift_out(closed_today)

    session = ShiftSession(
        business_id=current_user.business, shift=body.shift, session_date=today,
        opened_at=now, opened_by_user_id=current_user.id,
    )
    db.add(session)
    await db.flush()
    logger.info("shift opened id=%s business=%s shift=%s", session.id, current_user.business, body.shift.value)
    return _shift_out(session)


@router.post("/shifts/{session_id}/close", response_model=ShiftSessionOut)
async def close_shift(session_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> ShiftSessionOut:
    session = await db.get(ShiftSession, session_id)
    if session is None or session.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shift session not found")
    session.closed_at = datetime.now(timezone.utc)
    await db.flush()
    logger.info("shift closed id=%s business=%s", session.id, current_user.business)
    return _shift_out(session)


@router.get("/shifts", response_model=list[ShiftSessionOut])
async def list_shifts(current_user: CurrentUserDep, db: DbDep) -> list[ShiftSessionOut]:
    """Today's shift sessions (open and closed) - the staff dashboard's shift tiles."""
    today = datetime.now(timezone.utc).date()
    rows = await db.execute(
        select(ShiftSession).where(
            ShiftSession.business_id == current_user.business,
            ShiftSession.session_date == today,
        )
    )
    return [_shift_out(s) for s in rows.scalars().all()]


# ── Kiosk ────────────────────────────────────────────────────────────────

class KioskConfigOut(BaseModel):
    enabled: bool
    token: str | None


@router.get("/kiosk", response_model=KioskConfigOut)
async def get_kiosk_config(current_user: CurrentUserDep, db: DbDep) -> KioskConfigOut:
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    return KioskConfigOut(enabled=business.kiosk_token is not None, token=business.kiosk_token)


@router.post("/kiosk/enable", response_model=KioskConfigOut)
async def enable_kiosk(current_user: CurrentUserDep, db: DbDep) -> KioskConfigOut:
    """Generates a new kiosk link, replacing any existing one - the same
    action a business uses both to turn kiosk check-in on for the first
    time and to revoke a leaked/shared link by rotating it."""
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    business.kiosk_token = secrets.token_urlsafe(24)
    await db.flush()
    logger.info("kiosk enabled/rotated for business=%s", current_user.business)
    return KioskConfigOut(enabled=True, token=business.kiosk_token)


@router.post("/kiosk/disable", response_model=KioskConfigOut)
async def disable_kiosk(current_user: CurrentUserDep, db: DbDep) -> KioskConfigOut:
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    business.kiosk_token = None
    await db.flush()
    logger.info("kiosk disabled for business=%s", current_user.business)
    return KioskConfigOut(enabled=False, token=None)


# ── Queue entries ────────────────────────────────────────────────────────

class CheckInIn(BaseModel):
    shift: Shift
    customer_id: str | None = None
    doctor_id: str | None = None


class QueueEntryOut(BaseModel):
    id: str
    customer_id: str | None
    doctor_id: str | None
    shift: str
    queue_date: date
    queue_number: int
    status: str
    checked_in_at: datetime
    called_at: datetime | None
    completed_at: datetime | None


def _out(q: QueueEntry) -> QueueEntryOut:
    return QueueEntryOut(
        id=str(q.id),
        customer_id=str(q.customer_id) if q.customer_id else None,
        doctor_id=str(q.doctor_id) if q.doctor_id else None,
        shift=q.shift.value if hasattr(q.shift, "value") else str(q.shift),
        queue_date=q.queue_date,
        queue_number=q.queue_number,
        status=q.status.value if hasattr(q.status, "value") else str(q.status),
        checked_in_at=q.checked_in_at,
        called_at=q.called_at,
        completed_at=q.completed_at,
    )


@router.post("/check-in", response_model=QueueEntryOut, status_code=status.HTTP_201_CREATED)
async def check_in(body: CheckInIn, current_user: CurrentUserDep, db: DbDep) -> QueueEntryOut:
    customer: Customer | None = None
    if body.customer_id:
        customer = await db.get(Customer, uuid.UUID(body.customer_id))
        if customer is None or customer.business_id != current_user.business:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")

    doctor_id = uuid.UUID(body.doctor_id) if body.doctor_id else None

    try:
        entry = await queue_booking.issue_token(
            db,
            business_id=current_user.business,
            shift=body.shift,
            customer_id=customer.id if customer else None,
            doctor_id=doctor_id,
            intake_channel=IntakeChannel.manual,
        )
    except queue_booking.ShiftNotOpen as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return _out(entry)


@router.get("", response_model=list[QueueEntryOut])
async def list_queue(
    current_user: CurrentUserDep,
    db: DbDep,
    doctor_id: str | None = None,
    shift: Shift | None = None,
    status_filter: QueueStatus | None = Query(default=None, alias="status"),
    queue_date: date | None = None,
) -> list[QueueEntryOut]:
    target_date = queue_date or datetime.now(timezone.utc).date()
    query = select(QueueEntry).where(
        QueueEntry.business_id == current_user.business,
        QueueEntry.queue_date == target_date,
    ).order_by(QueueEntry.shift.asc(), QueueEntry.queue_number.asc())
    if doctor_id:
        query = query.where(QueueEntry.doctor_id == uuid.UUID(doctor_id))
    if shift:
        query = query.where(QueueEntry.shift == shift)
    if status_filter:
        query = query.where(QueueEntry.status == status_filter)
    rows = await db.execute(query)
    return [_out(q) for q in rows.scalars().all()]


class QueuePatch(BaseModel):
    status: QueueStatus


@router.patch("/{entry_id}", response_model=QueueEntryOut)
async def update_queue_entry(entry_id: uuid.UUID, body: QueuePatch, current_user: CurrentUserDep, db: DbDep) -> QueueEntryOut:
    entry = await db.get(QueueEntry, entry_id)
    if entry is None or entry.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Queue entry not found")

    now = datetime.now(timezone.utc)
    entry.status = body.status
    if body.status == QueueStatus.in_consultation and entry.called_at is None:
        entry.called_at = now
    if body.status in (QueueStatus.done, QueueStatus.skipped, QueueStatus.cancelled):
        entry.completed_at = now

    await db.flush()
    return _out(entry)
