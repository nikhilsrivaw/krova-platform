"""
The dashboard's own view of shared/ai/agent.py's notify_escalation()
records - a small acknowledge workflow, the same "a person marks a queue
item handled" shape the draft-approval flow already has, not a new
interaction pattern. Acknowledging here is what stops
shared/care/escalation_failsafe.py's SMS sweep from firing for this one.
"""

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import Business, Escalation, User
from shared.team import conflict
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/escalations", tags=["escalations"])


class EscalationOut(BaseModel):
    id: str
    customer_id: str | None
    channel: str
    reason: str
    category: str | None
    request_summary: str | None
    caller_phone: str | None
    contact_handle: str | None
    status: str
    due_at: datetime | None
    resolved_at: datetime | None
    resolution_note: str | None
    created_at: datetime
    acknowledged_at: datetime | None
    escalated_further_at: datetime | None
    assigned_to_user_id: str | None = None
    assigned_to_name: str | None = None
    assigned_at: datetime | None = None


class EscalationStatusIn(BaseModel):
    status: Literal["in_progress", "resolved", "dismissed"]
    resolution_note: str | None = None


def _out(e: Escalation, names: dict | None = None) -> EscalationOut:
    return EscalationOut(
        assigned_to_user_id=str(e.assigned_to_user_id) if e.assigned_to_user_id else None,
        assigned_to_name=(names or {}).get(e.assigned_to_user_id),
        assigned_at=e.assigned_at,
        id=str(e.id), customer_id=str(e.customer_id) if e.customer_id else None,
        channel=e.channel, reason=e.reason, category=e.category,
        request_summary=e.request_summary, caller_phone=e.caller_phone, contact_handle=e.contact_handle,
        status=e.status, due_at=e.due_at, resolved_at=e.resolved_at,
        resolution_note=e.resolution_note, created_at=e.created_at,
        acknowledged_at=e.acknowledged_at, escalated_further_at=e.escalated_further_at,
    )


@router.get("", response_model=list[EscalationOut])
async def list_escalations(
    current_user: CurrentUserDep, db: DbDep, acknowledged: bool = Query(default=False),
    mine: bool = Query(default=False), unassigned: bool = Query(default=False),
) -> list[EscalationOut]:
    query = select(Escalation).where(Escalation.business_id == current_user.business)
    query = query.where(Escalation.acknowledged_at.is_not(None) if acknowledged else Escalation.acknowledged_at.is_(None))
    if mine:
        query = query.where(Escalation.assigned_to_user_id == current_user.id)
    elif unassigned:
        query = query.where(Escalation.assigned_to_user_id.is_(None))
    query = query.order_by(Escalation.created_at.desc())
    items = (await db.execute(query)).scalars().all()
    ids = {e.assigned_to_user_id for e in items if e.assigned_to_user_id}
    names: dict = {}
    if ids:
        users = (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()
        names = {u.id: (u.full_name or u.username or u.email) for u in users}
    return [_out(e, names) for e in items]


@router.get("/count")
async def pending_count(current_user: CurrentUserDep, db: DbDep) -> dict:
    """For the sidebar badge - same shape as approvals.py's own /count."""
    result = await db.execute(select(func.count(Escalation.id)).where(
        Escalation.business_id == current_user.business,
        Escalation.acknowledged_at.is_(None),
    ))
    return {"open": int(result.scalar_one())}


class EscalationSettingsOut(BaseModel):
    escalation_sla_hours: int | None
    outbound_number_series: str | None


class EscalationSettingsIn(BaseModel):
    escalation_sla_hours: int | None = Field(default=None, ge=1, le=168)
    outbound_number_series: Literal["080", "022", "140"] | None = None


@router.get("/settings", response_model=EscalationSettingsOut)
async def get_escalation_settings(current_user: CurrentUserDep, db: DbDep) -> EscalationSettingsOut:
    business = await db.get(Business, current_user.business)
    settings = (business.settings or {}) if business else {}
    return EscalationSettingsOut(
        escalation_sla_hours=settings.get("escalation_sla_hours"),
        outbound_number_series=settings.get("outbound_number_series"),
    )


@router.patch("/settings", response_model=EscalationSettingsOut)
async def set_escalation_settings(
    body: EscalationSettingsIn, current_user: CurrentUserDep, db: DbDep,
) -> EscalationSettingsOut:
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    business.settings = {
        **(business.settings or {}),
        "escalation_sla_hours": body.escalation_sla_hours,
        "outbound_number_series": body.outbound_number_series,
    }
    await db.flush()
    return EscalationSettingsOut(
        escalation_sla_hours=body.escalation_sla_hours,
        outbound_number_series=body.outbound_number_series,
    )


@router.patch("/{escalation_id}/status", response_model=EscalationOut)
async def set_escalation_status(
    escalation_id: uuid.UUID, body: EscalationStatusIn, current_user: CurrentUserDep, db: DbDep,
) -> EscalationOut:
    escalation = await db.get(Escalation, escalation_id)
    if escalation is None or escalation.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Escalation not found")
    await conflict.guard_escalation_change(
        db, escalation=escalation, actor_id=current_user.id, actor_role=current_user.role
    )
    now = datetime.now(timezone.utc)
    escalation.status = body.status
    if escalation.acknowledged_at is None:
        escalation.acknowledged_at = now
        escalation.acknowledged_by_user_id = current_user.id
    if body.status in ("resolved", "dismissed"):
        escalation.resolved_at = now
        escalation.resolution_note = (body.resolution_note or "").strip() or None
    await db.flush()
    return _out(escalation)


@router.post("/{escalation_id}/acknowledge", response_model=EscalationOut)
async def acknowledge_escalation(escalation_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> EscalationOut:
    escalation = await db.get(Escalation, escalation_id)
    if escalation is None or escalation.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Escalation not found")

    if escalation.acknowledged_at is None:
        escalation.acknowledged_at = datetime.now(timezone.utc)
        escalation.acknowledged_by_user_id = current_user.id
        await db.flush()

    return _out(escalation)


class ClaimIn(BaseModel):
    # Owner/admin only: take it even though a teammate holds it.
    force: bool = False


@router.post("/{escalation_id}/claim", response_model=EscalationOut)
async def claim_escalation(
    escalation_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep, body: ClaimIn | None = None,
) -> EscalationOut:
    """
    "I've got this." One person holds an escalation; a second agent is told who.
    Taking an unheld one also acknowledges it, which stops the SMS failsafe.
    """
    escalation = await conflict.claim_escalation(
        db, business_id=current_user.business, escalation_id=escalation_id,
        actor_id=current_user.id, actor_role=current_user.role, force=bool(body and body.force),
    )
    await db.flush()
    user = await db.get(User, escalation.assigned_to_user_id) if escalation.assigned_to_user_id else None
    return _out(escalation, {user.id: (user.full_name or user.username or user.email)} if user else None)


@router.post("/{escalation_id}/release", response_model=EscalationOut)
async def release_escalation(escalation_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> EscalationOut:
    """Hand it back so anyone can take it. Only the holder, or an owner/admin."""
    escalation = await db.get(Escalation, escalation_id)
    if escalation is None or escalation.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Escalation not found")
    if escalation.assigned_to_user_id not in (None, current_user.id) and current_user.role not in conflict.SUPERVISORS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the person holding this, or an admin, can hand it back.")
    escalation.assigned_to_user_id = None
    escalation.assigned_at = None
    if escalation.status == "in_progress":
        escalation.status = "open"
    await db.flush()
    return _out(escalation)
