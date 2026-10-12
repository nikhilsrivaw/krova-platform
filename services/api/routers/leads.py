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
from shared.db.models import Call, Customer, Direction, InboundLead, Message, User

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
    # When a person (not the AI, not an automation) first answered this lead by message or call;
    # None = nobody has yet. The gap to received_at is the speed-to-lead that decides most sales.
    first_touch_at: datetime | None = None
    assigned_to_name: str | None = None


class LeadListOut(BaseModel):
    items: list[LeadOut]
    total: int


class LeadSourceCountOut(BaseModel):
    source: str
    total: int


def first_touch(received_at: datetime, touches: list[datetime]) -> datetime | None:
    """The earliest human contact at or after the lead arrived."""
    later = [t for t in touches if t >= received_at]
    return min(later) if later else None


async def _human_touches(db, business_id, rows: list[InboundLead]) -> dict:
    """customer_id -> times a person messaged or called them, since the oldest lead on the page."""
    ids = {r.customer_id for r in rows if r.customer_id}
    if not ids:
        return {}
    since = min(r.received_at for r in rows)
    touches: dict = {i: [] for i in ids}
    messages = await db.execute(
        select(Message.customer_id, Message.occurred_at).where(
            Message.business_id == business_id, Message.customer_id.in_(ids),
            Message.direction == Direction.outbound, Message.sent_by_user_id.is_not(None),
            Message.occurred_at >= since,
        )
    )
    for customer_id, at in messages.all():
        touches[customer_id].append(at)
    calls = await db.execute(
        select(Call.customer_id, Call.started_at).where(
            Call.business_id == business_id, Call.customer_id.in_(ids),
            Call.handled_by_user_id.is_not(None), Call.started_at >= since,
        )
    )
    for customer_id, at in calls.all():
        touches[customer_id].append(at)
    return touches


def _to_out(row: InboundLead, touches: dict | None = None, owner_names: dict | None = None) -> LeadOut:
    return LeadOut(
        id=str(row.id), source=row.source, name=row.name, phone=row.phone, email=row.email,
        query=row.query, status=row.status, customer_id=str(row.customer_id) if row.customer_id else None,
        received_at=row.received_at,
        first_touch_at=first_touch(row.received_at, (touches or {}).get(row.customer_id, [])) if row.customer_id else None,
        assigned_to_name=(owner_names or {}).get(row.customer_id),
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
    touches = await _human_touches(db, current_user.business, list(rows))
    owner_names: dict = {}
    customer_ids = [r.customer_id for r in rows if r.customer_id]
    if customer_ids:
        owners = await db.execute(
            select(Customer.id, User.full_name, User.username)
            .join(User, User.id == Customer.assigned_to_user_id)
            .where(Customer.id.in_(customer_ids))
        )
        owner_names = {cid: (full or uname) for cid, full, uname in owners.all()}
    return LeadListOut(items=[_to_out(r, touches, owner_names) for r in rows], total=total)


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
