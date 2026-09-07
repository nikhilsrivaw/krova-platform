"""
The dashboard's own view of shared/ai/agent.py's notify_escalation()
records - a small acknowledge workflow, the same "a person marks a queue
item handled" shape the draft-approval flow already has, not a new
interaction pattern. Acknowledging here is what stops
shared/care/escalation_failsafe.py's SMS sweep from firing for this one.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import Escalation
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/escalations", tags=["escalations"])


class EscalationOut(BaseModel):
    id: str
    customer_id: str | None
    channel: str
    reason: str
    created_at: datetime
    acknowledged_at: datetime | None
    escalated_further_at: datetime | None


def _out(e: Escalation) -> EscalationOut:
    return EscalationOut(
        id=str(e.id), customer_id=str(e.customer_id) if e.customer_id else None,
        channel=e.channel, reason=e.reason, created_at=e.created_at,
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
