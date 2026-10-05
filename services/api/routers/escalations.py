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
from shared.db.models import Business, Escalation
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


class EscalationStatusIn(BaseModel):
    status: Literal["in_progress", "resolved", "dismissed"]
    resolution_note: str | None = None


def _out(e: Escalation) -> EscalationOut:
    return EscalationOut(
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
) -> list[EscalationOut]:
    query = select(Escalation).where(Escalation.business_id == current_user.business)
    query = query.where(Escalation.acknowledged_at.is_not(None) if acknowledged else Escalation.acknowledged_at.is_(None))
    query = query.order_by(Escalation.created_at.desc())
    rows = await db.execute(query)
    return [_out(e) for e in rows.scalars().all()]


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
