"""
Where a founder actually reads what shared/ai/signals.py found: bugs,
feature requests, complaints, churn risk, praise - Insight rows that existed
in the schema with nowhere to be read until this.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import Insight
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/signals", tags=["signals"])


class SignalOut(BaseModel):
    id: str
    customer_id: str | None
    kind: str
    title: str
    body: str | None
    severity: str
    created_at: datetime
    dismissed_at: datetime | None


def _out(i: Insight) -> SignalOut:
    return SignalOut(
        id=str(i.id), customer_id=str(i.customer_id) if i.customer_id else None,
        kind=i.kind, title=i.title, body=i.body, severity=i.severity,
        created_at=i.created_at, dismissed_at=i.dismissed_at,
    )


@router.get("", response_model=list[SignalOut])
async def list_signals(
    current_user: CurrentUserDep,
    db: DbDep,
    kind: str | None = None,
    severity: str | None = None,
    include_dismissed: bool = Query(default=False),
) -> list[SignalOut]:
    query = select(Insight).where(Insight.business_id == current_user.business)
    if kind:
        query = query.where(Insight.kind == kind)
    if severity:
        query = query.where(Insight.severity == severity)
    if not include_dismissed:
        query = query.where(Insight.dismissed_at.is_(None))
    query = query.order_by(Insight.created_at.desc())

    rows = await db.execute(query)
    return [_out(i) for i in rows.scalars().all()]


@router.post("/{signal_id}/dismiss", response_model=SignalOut)
async def dismiss_signal(signal_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> SignalOut:
    signal = await db.get(Insight, signal_id)
    if signal is None or signal.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Signal not found")
    from datetime import timezone

    signal.dismissed_at = datetime.now(timezone.utc)
    await db.flush()
    return _out(signal)
