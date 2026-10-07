"""
One list across every lead source: Justdial, IndiaMART, a portal webhook
(Magicbricks/99Acres/Housing.com/any other tool), a CSV/Excel upload, a
manually typed lead, or a forwarded email. All of them write InboundLead
through the same shared/leads/intake.py, so this is a single read, not a
union of per-source endpoints.
"""

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import InboundLead

router = APIRouter(prefix="/leads", tags=["leads"])

MAX_LIMIT = 100


class LeadOut(BaseModel):
    id: str
    source: str
    name: str | None
    phone: str | None
    email: str | None
    query: str | None
    status: str
    customer_id: str | None
    received_at: datetime


class LeadListOut(BaseModel):
    items: list[LeadOut]
    total: int


class LeadSourceCountOut(BaseModel):
    source: str
    total: int


def _to_out(row: InboundLead) -> LeadOut:
    return LeadOut(
        id=str(row.id), source=row.source, name=row.name, phone=row.phone, email=row.email,
        query=row.query, status=row.status, customer_id=str(row.customer_id) if row.customer_id else None,
        received_at=row.received_at,
    )


@router.get("", response_model=LeadListOut)
async def list_leads(
    current_user: CurrentUserDep,
    db: DbDep,
    source: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> LeadListOut:
    limit = min(max(limit, 1), MAX_LIMIT)
    offset = max(offset, 0)

    conditions = [InboundLead.business_id == current_user.business]
    if source:
        sources = [s.strip() for s in source.split(",") if s.strip()]
        if sources:
            conditions.append(InboundLead.source.in_(sources))
    if status:
        conditions.append(InboundLead.status == status)
    if q:
        like = f"%{q.strip()}%"
        conditions.append(or_(
            InboundLead.name.ilike(like), InboundLead.phone.ilike(like),
            InboundLead.email.ilike(like), InboundLead.query.ilike(like),
        ))

    total = (await db.execute(select(func.count(InboundLead.id)).where(*conditions))).scalar_one()
    rows = (
        await db.execute(
            select(InboundLead).where(*conditions)
            .order_by(InboundLead.received_at.desc())
            .limit(limit).offset(offset)
        )
    ).scalars().all()
    return LeadListOut(items=[_to_out(r) for r in rows], total=total)


@router.get("/sources", response_model=list[LeadSourceCountOut])
async def list_lead_sources_with_counts(current_user: CurrentUserDep, db: DbDep) -> list[LeadSourceCountOut]:
    """Which source values this business actually has leads from, for a filter dropdown."""
    rows = await db.execute(
        select(InboundLead.source, func.count(InboundLead.id))
        .where(InboundLead.business_id == current_user.business)
        .group_by(InboundLead.source)
        .order_by(func.count(InboundLead.id).desc())
    )
    return [LeadSourceCountOut(source=s, total=c) for s, c in rows.all()]
