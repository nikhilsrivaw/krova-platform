"""
The OPD Queue capability's door: open/close today's shifts, check someone
in, see today's live list, call the next one. What a business calls any of
this - patient, guest, applicant - is its own setting, resolved by
shared/verticals/labels.py. See shared/db/models/queue.py
and shared/db/models/shift.py for why this is not the Scheduling capability
with different labels, and shared/scheduling/queue_booking.py for the
shared "issue a token" logic every entry point (this router, the public
kiosk, the voice/WhatsApp agent) goes through.
"""

import secrets
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared import verticals
from shared.db.models import Business, Customer, IntakeChannel, QueueEntry, QueueStatus, Shift, ShiftSession
from shared.integrations import google_calendar
from shared.scheduling import notify, queue_booking
from shared.verticals import labels
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/queue", tags=["queue"])

_TURN_NEAR_THRESHOLD_DEFAULT = 2


def _turn_near_threshold(business: Business) -> int:
    """
    How many ahead counts as "your turn is near" - a per-business setting,
    because two tables ahead at a restaurant is nothing like two patients
    ahead at a clinic.

    Still a count and not a time estimate: no per-token duration data exists
    anywhere in this system to base one on (same reasoning this codebase
    already uses for post_call_actions.py's _MAX_DELAY_SECONDS). Guarded on
    read because this runs inside the live "call next" action - junk in the
    JSONB bag must not 500 a real queue advance.
    """
    raw = ((business.settings or {}).get("queue") or {}).get("turn_near_threshold")
    return raw if isinstance(raw, int) and raw >= 1 else _TURN_NEAR_THRESHOLD_DEFAULT


async def _require_opd_queue(business_id: uuid.UUID, db: DbDep) -> Business:
    """
    Shift open/close and kiosk enable/disable don't route through
    issue_token() (they never create a QueueEntry), so they need their
    own capability check - queue_booking.issue_token's own new
    OpdQueueNotEnabled guard doesn't reach them. Same check, same 403,
    just here since there's no shared function to fix once for these.
    """
    business = await db.get(Business, business_id)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    if not verticals.has_capability(business, "opd_queue"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This business does not have the opd_queue capability")
    return business


# ── Settings ─────────────────────────────────────────────────────────────

class QueueShiftLabelsIn(BaseModel):
    morning: str | None = Field(default=None, max_length=40)
    evening: str | None = Field(default=None, max_length=40)
    emergency: str | None = Field(default=None, max_length=40)


class QueueLabelsIn(BaseModel):
    person: str | None = Field(default=None, max_length=40)
    ticket: str | None = Field(default=None, max_length=40)
    serving: str | None = Field(default=None, max_length=40)
    shifts: QueueShiftLabelsIn | None = None


class QueueSettingsIn(BaseModel):
    enabled: bool | None = None
    labels: QueueLabelsIn | None = None
    turn_near_threshold: int | None = Field(default=None, ge=1, le=50)


class QueueSettingsOut(BaseModel):
    enabled: bool
    labels: dict
    turn_near_threshold: int


def _settings_out(business: Business) -> QueueSettingsOut:
    return QueueSettingsOut(
        enabled=verticals.has_capability(business, "opd_queue"),
        labels=labels.queue_labels(business),
        turn_near_threshold=_turn_near_threshold(business),
    )


@router.get("/settings", response_model=QueueSettingsOut)
async def get_queue_settings(current_user: CurrentUserDep, db: DbDep) -> QueueSettingsOut:
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    return _settings_out(business)


@router.put("/settings", response_model=QueueSettingsOut)
async def update_queue_settings(
    body: QueueSettingsIn, current_user: CurrentUserDep, db: DbDep
) -> QueueSettingsOut:
    """
    Turn the queue on for this business and set what it calls things.

    Deliberately NOT behind _require_opd_queue: this is the endpoint a
    business uses to turn the capability on in the first place, so gating it
    on already having the capability would make it unreachable for exactly
    the businesses it exists for.

    Named fields rather than a settings passthrough, for the reason
    auth.py's UpdateMeRequest already gives: Business.settings is the
    storage, not an arbitrary-JSONB write surface.
    """
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")

    settings = {**(business.settings or {})}

    if body.enabled is not None:
        overrides = dict(settings.get("capability_overrides") or {})
        overrides["opd_queue"] = body.enabled
        settings["capability_overrides"] = overrides

    queue_cfg = dict(settings.get("queue") or {})
    if body.labels is not None:
        # Replaced wholesale, not merged: the Settings form posts every
        # label field together, and a field cleared there must actually
        # clear rather than leave the old word stranded in the bag. A
        # cleared field then falls back through labels.queue_labels' own
        # tiers, which is what makes "blank" mean "use the default".
        queue_cfg["labels"] = body.labels.model_dump(exclude_none=True)
    if body.turn_near_threshold is not None:
        queue_cfg["turn_near_threshold"] = body.turn_near_threshold
    if queue_cfg:
        settings["queue"] = queue_cfg

    business.settings = settings
    await db.commit()
    logger.info("queue settings updated for business=%s", current_user.business)
    return _settings_out(business)


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
    await _require_opd_queue(current_user.business, db)
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
    await _require_opd_queue(current_user.business, db)
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
    business = await _require_opd_queue(current_user.business, db)
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
    except queue_booking.OpdQueueNotEnabled as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

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
    business = await _require_opd_queue(current_user.business, db)
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

    if body.status == QueueStatus.cancelled and entry.google_calendar_event_id:
        try:
            await google_calendar.sync_queue_entry(db, business=business, entry=entry, action="cancel")
        except Exception:
            logger.exception("calendar cancel-sync failed for queue entry=%s", entry.id)

    # The real fix for queue_checkin_confirmation's own "we'll notify you
    # as your turn nears" promise - nothing used to keep it. Only checked
    # when a token gets called in (the queue actually advanced), not on
    # every patch - a skip/cancel doesn't move anyone's real position the
    # same way.
    if body.status == QueueStatus.in_consultation:
        await _notify_if_turn_near(entry, db)

    return _out(entry)


async def _notify_if_turn_near(called_entry: QueueEntry, db: DbDep) -> None:
    business = await db.get(Business, called_entry.business_id)
    if business is None:
        return
    threshold = _turn_near_threshold(business)

    waiting = (
        await db.execute(
            select(QueueEntry).where(
                QueueEntry.business_id == called_entry.business_id,
                QueueEntry.queue_date == called_entry.queue_date,
                QueueEntry.shift == called_entry.shift,
                QueueEntry.status == QueueStatus.waiting,
            ).order_by(QueueEntry.queue_number.asc())
        )
    ).scalars().all()
    if len(waiting) < threshold:
        return

    near = waiting[threshold - 1]
    if near.turn_near_notified_at is not None or near.customer_id is None:
        return

    customer = await db.get(Customer, near.customer_id)
    if customer is None:
        return

    # Stamped regardless of whether the send actually succeeded - same
    # "never retried forever, failure logged loudly instead" shape
    # Escalation.escalated_further_at already uses. A notification
    # failure must never block or undo the queue's own real advance.
    near.turn_near_notified_at = datetime.now(timezone.utc)
    try:
        await notify.send_queue_turn_near(
            db, business=business, customer=customer,
            queue_number=near.queue_number, tokens_ahead=threshold - 1,
        )
    except Exception:
        logger.exception("queue turn-near notification failed for entry=%s", near.id)
