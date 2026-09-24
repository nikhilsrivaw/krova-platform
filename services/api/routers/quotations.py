"""
Quotations: the offers a B2B business makes, and whether they ever close.

The gap this fills, from docs/new/type-1-research.md: a quote is sent in a
WhatsApp thread and then tracked nowhere, and deals left sitting past 21
days win 70% less often. The follow-up sweep
(shared/care/quotation_followup.py) is where the value actually lands;
this router is how quotes get in and how outcomes get recorded.
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from services.api.dependencies import CurrentUserDep, DbDep
from shared import verticals
from shared.db.models import (
    Business,
    Customer,
    Quotation,
    QuotationItem,
    QuotationStatus,
)
from shared.db.models.quotation import OPEN_STATUSES

router = APIRouter(prefix="/quotations", tags=["quotations"])


async def _require_quotations(business_id: uuid.UUID, db: DbDep) -> Business:
    """Same self-gating shape as orders.py/products.py."""
    business = await db.get(Business, business_id)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    if not verticals.has_capability(business, "quotations"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This business does not have the quotations capability"
        )
    return business


class ItemIn(BaseModel):
    description: str = Field(min_length=1)
    quantity: str | None = None
    unit_price_paise: int | None = None
    line_total_paise: int | None = None
    variant_id: uuid.UUID | None = None


class ItemOut(BaseModel):
    id: uuid.UUID
    description: str
    quantity: str | None
    unit_price_paise: int | None
    line_total_paise: int | None
    variant_id: uuid.UUID | None


class QuotationIn(BaseModel):
    customer_id: uuid.UUID
    reference: str | None = Field(default=None, max_length=80)
    total_paise: int | None = None
    valid_until: datetime | None = None
    notes: str | None = None
    items: list[ItemIn] = Field(default_factory=list)
    # A quote typed in after it was already sent is the common case, so
    # this defaults to sent rather than draft.
    mark_sent: bool = True


class QuotationOut(BaseModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    customer_name: str | None
    reference: str | None
    status: QuotationStatus
    total_paise: int | None
    currency: str
    sent_at: datetime | None
    valid_until: datetime | None
    notes: str | None
    follow_up_count: int
    last_followed_up_at: datetime | None
    closed_at: datetime | None
    outcome_note: str | None
    supersedes_id: uuid.UUID | None
    source_quote: str | None
    items: list[ItemOut]
    # Days since it was sent - the number the 21-day research is about,
    # computed here so every consumer reads the same definition.
    days_open: int | None


class OutcomeIn(BaseModel):
    status: QuotationStatus
    outcome_note: str | None = None


class Pipeline(BaseModel):
    open_count: int
    open_value_paise: int
    won_count: int
    lost_count: int
    expired_count: int
    # Won / (won + lost), ignoring still-open and expired-without-answer.
    win_rate: float | None
    stale_count: int
    note: str


def _days_open(quotation: Quotation) -> int | None:
    if quotation.sent_at is None:
        return None
    return (datetime.now(timezone.utc) - quotation.sent_at).days


def _to_out(q: Quotation, items: list[QuotationItem], customer_name: str | None) -> QuotationOut:
    return QuotationOut(
        id=q.id,
        customer_id=q.customer_id,
        customer_name=customer_name,
        reference=q.reference,
        status=q.status,
        total_paise=q.total_paise,
        currency=q.currency,
        sent_at=q.sent_at,
        valid_until=q.valid_until,
        notes=q.notes,
        follow_up_count=q.follow_up_count,
        last_followed_up_at=q.last_followed_up_at,
        closed_at=q.closed_at,
        outcome_note=q.outcome_note,
        supersedes_id=q.supersedes_id,
        source_quote=q.source_quote,
        days_open=_days_open(q),
        items=[
            ItemOut(
                id=i.id,
                description=i.description,
                quantity=i.quantity,
                unit_price_paise=i.unit_price_paise,
                line_total_paise=i.line_total_paise,
                variant_id=i.variant_id,
            )
            for i in items
        ],
    )


async def _load_items(
    quotation_ids: list[uuid.UUID], db: DbDep
) -> dict[uuid.UUID, list[QuotationItem]]:
    if not quotation_ids:
        return {}
    rows = (
        await db.execute(
            select(QuotationItem)
            .where(QuotationItem.quotation_id.in_(quotation_ids))
            .order_by(QuotationItem.position)
        )
    ).scalars().all()
    grouped: dict[uuid.UUID, list[QuotationItem]] = {}
    for row in rows:
        grouped.setdefault(row.quotation_id, []).append(row)
    return grouped


@router.get("", response_model=list[QuotationOut])
async def list_quotations(
    current_user: CurrentUserDep,
    db: DbDep,
    status_filter: QuotationStatus | None = Query(default=None, alias="status"),
    open_only: bool = Query(default=False),
    customer_id: uuid.UUID | None = None,
    limit: int = Query(default=100, le=500),
) -> list[QuotationOut]:
    """Quotes, newest first. `open_only` is the sweep's own definition of open."""
    await _require_quotations(current_user.business, db)

    query = select(Quotation).where(Quotation.business_id == current_user.business)
    if status_filter is not None:
        query = query.where(Quotation.status == status_filter)
    if open_only:
        query = query.where(Quotation.status.in_(OPEN_STATUSES))
    if customer_id is not None:
        query = query.where(Quotation.customer_id == customer_id)

    quotations = (
        await db.execute(query.order_by(Quotation.created_at.desc()).limit(limit))
    ).scalars().all()
    if not quotations:
        return []

    items = await _load_items([q.id for q in quotations], db)
    names = {
        c.id: c.display_name
        for c in (
            await db.execute(
                select(Customer).where(Customer.id.in_([q.customer_id for q in quotations]))
            )
        ).scalars().all()
    }
    return [_to_out(q, items.get(q.id, []), names.get(q.customer_id)) for q in quotations]


@router.get("/pipeline", response_model=Pipeline)
async def pipeline(
    current_user: CurrentUserDep, db: DbDep, stale_days: int = Query(default=21, le=365)
) -> Pipeline:
    """
    The position in one call.

    `stale_days` defaults to 21 because that is the threshold the research
    is about - past it, win rate drops roughly 70%.
    """
    await _require_quotations(current_user.business, db)
    mine = Quotation.business_id == current_user.business

    async def _count(*where) -> int:
        result = await db.execute(select(func.count(Quotation.id)).where(mine, *where))
        return int(result.scalar_one())

    open_count = await _count(Quotation.status.in_(OPEN_STATUSES))
    open_value = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(Quotation.total_paise), 0)).where(
                    mine, Quotation.status.in_(OPEN_STATUSES)
                )
            )
        ).scalar_one()
    )
    won = await _count(Quotation.status == QuotationStatus.won)
    lost = await _count(Quotation.status == QuotationStatus.lost)
    expired = await _count(Quotation.status == QuotationStatus.expired)

    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    stale = await _count(
        Quotation.status.in_(OPEN_STATUSES),
        Quotation.sent_at.is_not(None),
        Quotation.sent_at < cutoff,
    )

    decided = won + lost
    return Pipeline(
        open_count=open_count,
        open_value_paise=open_value,
        won_count=won,
        lost_count=lost,
        expired_count=expired,
        win_rate=(won / decided) if decided else None,
        stale_count=stale,
        note=(
            f"{stale} open quote(s) older than {stale_days} days. "
            "Deals left past this point close far less often."
        ),
    )


@router.post("", response_model=QuotationOut, status_code=status.HTTP_201_CREATED)
async def create_quotation(
    body: QuotationIn, current_user: CurrentUserDep, db: DbDep
) -> QuotationOut:
    await _require_quotations(current_user.business, db)

    customer = await db.get(Customer, body.customer_id)
    if customer is None or customer.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")

    now = datetime.now(timezone.utc)
    quotation = Quotation(
        business_id=current_user.business,
        customer_id=body.customer_id,
        reference=body.reference,
        status=QuotationStatus.sent if body.mark_sent else QuotationStatus.draft,
        total_paise=body.total_paise,
        sent_at=now if body.mark_sent else None,
        valid_until=body.valid_until,
        notes=body.notes,
        source_message_ids=[],
    )
    db.add(quotation)
    await db.flush()

    for position, item in enumerate(body.items):
        db.add(
            QuotationItem(
                quotation_id=quotation.id,
                business_id=current_user.business,
                variant_id=item.variant_id,
                description=item.description,
                quantity=item.quantity,
                unit_price_paise=item.unit_price_paise,
                line_total_paise=item.line_total_paise,
                position=position,
            )
        )
    await db.commit()

    items = await _load_items([quotation.id], db)
    return _to_out(quotation, items.get(quotation.id, []), customer.display_name)


@router.post("/{quotation_id}/outcome", response_model=QuotationOut)
async def record_outcome(
    quotation_id: uuid.UUID, body: OutcomeIn, current_user: CurrentUserDep, db: DbDep
) -> QuotationOut:
    """
    Close a quote out, or move it back to an open state.

    `closed_at` is stamped only for terminal states - reopening a quote
    someone closed by mistake should not leave a close date behind.
    """
    await _require_quotations(current_user.business, db)

    quotation = await db.get(Quotation, quotation_id)
    if quotation is None or quotation.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quotation not found")

    quotation.status = body.status
    quotation.outcome_note = body.outcome_note
    quotation.closed_at = (
        None if body.status in OPEN_STATUSES else datetime.now(timezone.utc)
    )
    await db.commit()

    items = await _load_items([quotation.id], db)
    customer = await db.get(Customer, quotation.customer_id)
    return _to_out(quotation, items.get(quotation.id, []), customer.display_name if customer else None)


@router.post("/{quotation_id}/revise", response_model=QuotationOut, status_code=status.HTTP_201_CREATED)
async def revise_quotation(
    quotation_id: uuid.UUID, body: QuotationIn, current_user: CurrentUserDep, db: DbDep
) -> QuotationOut:
    """
    Supersede a quote with a revised one.

    The original is kept and marked withdrawn rather than edited - what was
    first offered is often the most useful thing to look back at in a
    negotiation, and editing destroys it.
    """
    await _require_quotations(current_user.business, db)

    original = await db.get(Quotation, quotation_id)
    if original is None or original.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quotation not found")

    now = datetime.now(timezone.utc)
    revision = Quotation(
        business_id=current_user.business,
        customer_id=original.customer_id,
        reference=body.reference,
        status=QuotationStatus.sent if body.mark_sent else QuotationStatus.draft,
        total_paise=body.total_paise,
        sent_at=now if body.mark_sent else None,
        valid_until=body.valid_until,
        notes=body.notes,
        supersedes_id=original.id,
        source_message_ids=[],
    )
    db.add(revision)
    original.status = QuotationStatus.withdrawn
    original.closed_at = now
    await db.flush()

    for position, item in enumerate(body.items):
        db.add(
            QuotationItem(
                quotation_id=revision.id,
                business_id=current_user.business,
                variant_id=item.variant_id,
                description=item.description,
                quantity=item.quantity,
                unit_price_paise=item.unit_price_paise,
                line_total_paise=item.line_total_paise,
                position=position,
            )
        )
    await db.commit()

    items = await _load_items([revision.id], db)
    customer = await db.get(Customer, revision.customer_id)
    return _to_out(revision, items.get(revision.id, []), customer.display_name if customer else None)
