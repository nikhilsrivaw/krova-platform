"""
The door a law firm actually walks through to use Case Tracking: open a
matter for a client, keep its status and next hearing current, see what's
coming up across every client.

Without this, shared/ai/context.py has nothing to read - a case only
becomes something the agent can honestly answer questions about once a
human has recorded it here.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import Case, CaseStatus, Customer
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/cases", tags=["cases"])


class CaseIn(BaseModel):
    customer_id: str
    title: str = Field(min_length=1, max_length=255)
    case_number: str | None = Field(default=None, max_length=100)
    opposing_party: str | None = Field(default=None, max_length=255)
    court: str | None = Field(default=None, max_length=255)
    next_hearing_at: datetime | None = None
    notes: str | None = None


class CasePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    case_number: str | None = None
    opposing_party: str | None = None
    court: str | None = None
    status: CaseStatus | None = None
    next_hearing_at: datetime | None = None
    notes: str | None = None


class CaseOut(BaseModel):
    id: str
    customer_id: str
    title: str
    case_number: str | None
    opposing_party: str | None
    court: str | None
    status: str
    next_hearing_at: datetime | None
    notes: str | None


def _out(c: Case) -> CaseOut:
    return CaseOut(
        id=str(c.id), customer_id=str(c.customer_id), title=c.title,
        case_number=c.case_number, opposing_party=c.opposing_party, court=c.court,
        status=c.status.value if hasattr(c.status, "value") else str(c.status),
        next_hearing_at=c.next_hearing_at, notes=c.notes,
    )


@router.get("", response_model=list[CaseOut])
async def list_cases(
    current_user: CurrentUserDep,
    db: DbDep,
    customer_id: str | None = None,
    status_filter: CaseStatus | None = Query(default=None, alias="status"),
) -> list[CaseOut]:
    query = select(Case).where(Case.business_id == current_user.business).order_by(
        Case.next_hearing_at.asc().nullslast()
    )
    if customer_id:
        query = query.where(Case.customer_id == uuid.UUID(customer_id))
    if status_filter:
        query = query.where(Case.status == status_filter)
    rows = await db.execute(query)
    return [_out(c) for c in rows.scalars().all()]


@router.post("", response_model=CaseOut, status_code=status.HTTP_201_CREATED)
async def create_case(body: CaseIn, current_user: CurrentUserDep, db: DbDep) -> CaseOut:
    customer = await db.get(Customer, uuid.UUID(body.customer_id))
    if customer is None or customer.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")

    case = Case(
        business_id=current_user.business,
        customer_id=customer.id,
        title=body.title,
        case_number=body.case_number,
        opposing_party=body.opposing_party,
        court=body.court,
        next_hearing_at=body.next_hearing_at,
        notes=body.notes,
    )
    db.add(case)
    await db.flush()
    logger.info("case created id=%s business=%s", case.id, current_user.business)
    return _out(case)


@router.patch("/{case_id}", response_model=CaseOut)
async def update_case(case_id: uuid.UUID, body: CasePatch, current_user: CurrentUserDep, db: DbDep) -> CaseOut:
    case = await db.get(Case, case_id)
    if case is None or case.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Case not found")
    for field in (
        "title", "case_number", "opposing_party", "court", "status", "next_hearing_at", "notes"
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(case, field, value)
    await db.flush()
    return _out(case)


@router.get("/upcoming-hearings", response_model=list[CaseOut])
async def upcoming_hearings(current_user: CurrentUserDep, db: DbDep, within_days: int = 14) -> list[CaseOut]:
    """What a lawyer actually opens this screen to see - the docket, not the case list."""
    from datetime import timedelta, timezone

    now = datetime.now(timezone.utc)
    rows = await db.execute(
        select(Case)
        .where(
            Case.business_id == current_user.business,
            Case.status != CaseStatus.closed,
            Case.next_hearing_at.is_not(None),
            Case.next_hearing_at >= now,
            Case.next_hearing_at <= now + timedelta(days=within_days),
        )
        .order_by(Case.next_hearing_at.asc())
    )
    return [_out(c) for c in rows.scalars().all()]
