"""
The ledger - what a business is owed, and what it owes.

This is the screen the product exists for. An owner opens Krova to answer one
question they cannot answer today: what has been promised, by whom, and what
is late. Their accounts will tell them weeks from now; the conversations
already know.

Two things make it different from every other number on a dashboard.

Every figure is clickable. `GET /ledger/commitments/{id}` returns the actual
messages the promise was read from. For a product whose whole premise is
telling people things about their money, being able to show the working is the
reason they will believe the number at all.

Nothing uncertain is presented as fact. Commitments the extractor was unsure
about arrive as `unconfirmed` and sit in a separate queue for the owner to
accept or reject. They never inflate a total.
"""

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import (
    Commitment,
    CommitmentDirection,
    CommitmentStatus,
    Customer,
    CustomerIdentity,
    CustomerIntelligence,
    CustomerTag,
    Message,
    TagStatus,
)
from shared.identity import importer

router = APIRouter(prefix="/ledger", tags=["ledger"])


class CommitmentOut(BaseModel):
    id: str
    direction: str
    kind: str
    description: str
    amount_paise: int | None
    amount_display: str | None
    currency: str
    due_at: str | None
    due_at_explicit: bool
    overdue: bool
    status: str
    confidence: float
    source_quote: str | None
    customer_id: str
    customer_name: str | None
    created_at: str


class LedgerSummary(BaseModel):
    owed_to_us_paise: int
    owed_by_us_paise: int
    overdue_count: int
    overdue_paise: int
    open_count: int
    # Kept separate from every total above: these are guesses awaiting a human,
    # and folding them into a figure would present uncertainty as fact.
    unconfirmed_count: int


class EvidenceMessage(BaseModel):
    id: str
    channel: str
    direction: str
    text: str | None
    occurred_at: str


class CommitmentDetail(CommitmentOut):
    evidence: list[EvidenceMessage]


def _rupees(paise: int | None) -> str | None:
    if paise is None:
        return None
    return f"₹{paise / 100:,.0f}"


def _to_out(c: Commitment, customer_name: str | None, now: datetime) -> CommitmentOut:
    status_value = c.status.value if hasattr(c.status, "value") else str(c.status)
    return CommitmentOut(
        id=str(c.id),
        direction=c.direction.value if hasattr(c.direction, "value") else str(c.direction),
        kind=c.kind.value if hasattr(c.kind, "value") else str(c.kind),
        description=c.description,
        amount_paise=c.amount_paise,
        amount_display=_rupees(c.amount_paise),
        currency=c.currency,
        due_at=c.due_at.isoformat() if c.due_at else None,
        due_at_explicit=c.due_at_explicit,
        overdue=bool(
            c.due_at and c.due_at < now and status_value == CommitmentStatus.open.value
        ),
        status=status_value,
        confidence=c.confidence,
        source_quote=c.source_quote,
        customer_id=str(c.customer_id),
        customer_name=customer_name,
        created_at=c.created_at.isoformat(),
    )


@router.get("/summary", response_model=LedgerSummary)
async def ledger_summary(current_user: CurrentUserDep, db: DbDep) -> LedgerSummary:
    """
    The position, in one call.

    Only confirmed, open commitments count toward money. An unconfirmed
    extraction is a question, not a receivable.
    """
    business_id = current_user.business
    now = datetime.now(timezone.utc)

    open_only = (
        Commitment.business_id == business_id,
        Commitment.status == CommitmentStatus.open,
    )

    async def total(direction: CommitmentDirection) -> int:
        result = await db.execute(
            select(func.coalesce(func.sum(Commitment.amount_paise), 0)).where(
                *open_only, Commitment.direction == direction
            )
        )
        return int(result.scalar_one())

    overdue = await db.execute(
        select(
            func.count(Commitment.id),
            func.coalesce(func.sum(Commitment.amount_paise), 0),
        ).where(*open_only, Commitment.due_at < now)
    )
    overdue_count, overdue_paise = overdue.one()

    open_count = await db.execute(select(func.count(Commitment.id)).where(*open_only))
    unconfirmed = await db.execute(
        select(func.count(Commitment.id)).where(
            Commitment.business_id == business_id,
            Commitment.status == CommitmentStatus.unconfirmed,
        )
    )

    return LedgerSummary(
        owed_to_us_paise=await total(CommitmentDirection.they_owe),
        owed_by_us_paise=await total(CommitmentDirection.we_owe),
        overdue_count=int(overdue_count),
        overdue_paise=int(overdue_paise),
        open_count=int(open_count.scalar_one()),
        unconfirmed_count=int(unconfirmed.scalar_one()),
    )


@router.get("/commitments", response_model=list[CommitmentOut])
async def list_commitments(
    current_user: CurrentUserDep,
    db: DbDep,
    direction: Literal["we_owe", "they_owe"] | None = None,
    status_filter: Literal["open", "met", "missed", "cancelled", "unconfirmed"] | None = Query(
        default=None, alias="status"
    ),
    overdue_only: bool = False,
    customer_id: uuid.UUID | None = None,
    limit: int = Query(default=50, le=200),
) -> list[CommitmentOut]:
    """
    Promises, most urgent first.

    Ordered by due date with undated ones last: what is late matters more than
    what is recent, which is the opposite of how a message list sorts.
    """
    now = datetime.now(timezone.utc)
    conditions = [Commitment.business_id == current_user.business]

    if direction:
        conditions.append(Commitment.direction == direction)
    if status_filter:
        conditions.append(Commitment.status == status_filter)
    if customer_id:
        conditions.append(Commitment.customer_id == customer_id)
    if overdue_only:
        conditions.extend(
            [Commitment.due_at < now, Commitment.status == CommitmentStatus.open]
        )

    result = await db.execute(
        select(Commitment, Customer.display_name)
        .join(Customer, Customer.id == Commitment.customer_id)
        .where(*conditions)
        .order_by(Commitment.due_at.asc().nullslast(), Commitment.created_at.desc())
        .limit(limit)
    )
    return [_to_out(c, name, now) for c, name in result.all()]


@router.get("/commitments/{commitment_id}", response_model=CommitmentDetail)
async def commitment_detail(
    commitment_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep
) -> CommitmentDetail:
    """
    One promise, with the messages it was read from.

    This is the endpoint that makes the number trustworthy: an owner can see
    exactly what was said, by whom, and when. Nobody else in this market can
    show that, because nobody else is reading the conversations.
    """
    commitment = await db.get(Commitment, commitment_id)
    if commitment is None or commitment.business_id != current_user.business:
        # Same response whether it does not exist or belongs to someone else -
        # a 403 here would confirm the id is real.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Commitment not found"
        )

    customer = await db.get(Customer, commitment.customer_id)
    evidence_rows = await db.execute(
        select(Message)
        .where(
            Message.id.in_(commitment.source_message_ids),
            Message.business_id == current_user.business,
        )
        .order_by(Message.occurred_at)
    )

    base = _to_out(commitment, customer.display_name if customer else None,
                   datetime.now(timezone.utc))
    return CommitmentDetail(
        **base.model_dump(),
        evidence=[
            EvidenceMessage(
                id=str(m.id),
                channel=m.channel.value if hasattr(m.channel, "value") else str(m.channel),
                direction=(
                    m.direction.value if hasattr(m.direction, "value") else str(m.direction)
                ),
                text=m.content,
                occurred_at=m.occurred_at.isoformat(),
            )
            for m in evidence_rows.scalars().all()
        ],
    )


class ResolveBody(BaseModel):
    outcome: Literal["met", "missed", "cancelled"]


@router.post("/commitments/{commitment_id}/confirm", response_model=CommitmentOut)
async def confirm_commitment(
    commitment_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep
) -> CommitmentOut:
    """
    Accept a commitment the extractor was unsure about.

    Until an owner does this, an uncertain promise counts toward nothing.
    """
    commitment = await _owned(commitment_id, current_user.business, db)
    commitment.status = CommitmentStatus.open
    commitment.confirmed_by_user_id = current_user.id
    commitment.confidence = 1.0
    customer = await db.get(Customer, commitment.customer_id)
    return _to_out(commitment, customer.display_name if customer else None,
                   datetime.now(timezone.utc))


@router.post("/commitments/{commitment_id}/resolve", response_model=CommitmentOut)
async def resolve_commitment(
    commitment_id: uuid.UUID, body: ResolveBody, current_user: CurrentUserDep, db: DbDep
) -> CommitmentOut:
    """Close a promise: paid, missed, or called off."""
    commitment = await _owned(commitment_id, current_user.business, db)
    commitment.status = body.outcome
    commitment.resolved_at = datetime.now(timezone.utc)
    commitment.confirmed_by_user_id = current_user.id
    customer = await db.get(Customer, commitment.customer_id)
    return _to_out(commitment, customer.display_name if customer else None,
                   datetime.now(timezone.utc))


@router.get("/customers", response_model=list[dict])
async def list_customers(
    current_user: CurrentUserDep, db: DbDep, limit: int = Query(default=50, le=200)
) -> list[dict]:
    """
    Everyone this business talks to, most recent first - with the compressed
    profile the nightly worker already computed, so the list carries a real
    health score and summary rather than the client inventing a placeholder.
    """
    result = await db.execute(
        select(Customer)
        .where(Customer.business_id == current_user.business)
        .order_by(Customer.last_contact_at.desc().nullslast())
        .limit(limit)
    )
    customers = list(result.scalars().all())
    if not customers:
        return []

    ids = [c.id for c in customers]

    intelligence = dict(
        (row.customer_id, row)
        for row in (
            await db.execute(
                select(CustomerIntelligence).where(CustomerIntelligence.customer_id.in_(ids))
            )
        ).scalars().all()
    )

    tags_by_customer: dict[uuid.UUID, list[str]] = {}
    for customer_id, label in (
        await db.execute(
            select(CustomerTag.customer_id, CustomerTag.label).where(
                CustomerTag.customer_id.in_(ids), CustomerTag.status == TagStatus.confirmed,
            )
        )
    ).all():
        tags_by_customer.setdefault(customer_id, []).append(label)

    out = []
    for c in customers:
        identities = await db.execute(
            select(CustomerIdentity).where(CustomerIdentity.customer_id == c.id)
        )
        open_count = await db.execute(
            select(func.count(Commitment.id)).where(
                Commitment.customer_id == c.id,
                Commitment.status == CommitmentStatus.open,
            )
        )
        intel = intelligence.get(c.id)
        out.append(
            {
                "id": str(c.id),
                "name": c.display_name,
                "identities": [
                    {
                        "kind": i.kind.value if hasattr(i.kind, "value") else str(i.kind),
                        "value": i.value,
                    }
                    for i in identities.scalars().all()
                ],
                "last_contact_at": (
                    c.last_contact_at.isoformat() if c.last_contact_at else None
                ),
                "open_commitments": int(open_count.scalar_one()),
                "is_private": c.is_private,
                "stage": c.stage,
                "deal_value_paise": c.deal_value_paise,
                "tags": tags_by_customer.get(c.id, []),
                "health_score": intel.health_score if intel else None,
                "outstanding_paise": intel.outstanding_paise if intel else 0,
                "summary": intel.summary if intel else None,
                "preferred_channel": intel.preferred_channel if intel else None,
            }
        )
    return out


class ContactImportRow(BaseModel):
    phone: str = Field(min_length=1, max_length=32)
    name: str | None = Field(default=None, max_length=255)


class ContactImportIn(BaseModel):
    contacts: list[ContactImportRow] = Field(min_length=1, max_length=importer.MAX_ROWS_PER_IMPORT)


class ContactImportRowOut(BaseModel):
    row_number: int
    phone: str
    outcome: str
    reason: str | None
    customer_id: str | None


class ContactImportOut(BaseModel):
    created: int
    already_existed: int
    invalid: int
    rows: list[ContactImportRowOut]


@router.post("/customers/import", response_model=ContactImportOut)
async def import_customers(
    body: ContactImportIn, current_user: CurrentUserDep, db: DbDep
) -> ContactImportOut:
    """
    Seed the customer list from an existing spreadsheet.

    A business switching from another platform, or with a customer base that
    has simply never texted this number, has customers to reach on day one -
    not customers who trickle in over the following weeks as they happen to
    write first. CSV parsing and column mapping happen in the browser; this
    takes the already-structured rows.
    """
    rows = [
        importer.ImportRow(row_number=i + 1, phone=c.phone, name=c.name)
        for i, c in enumerate(body.contacts)
    ]
    result = await importer.import_contacts(current_user.business, rows, db)
    return ContactImportOut(
        created=result.created,
        already_existed=result.already_existed,
        invalid=result.invalid,
        rows=[
            ContactImportRowOut(
                row_number=r.row_number, phone=r.phone, outcome=r.outcome,
                reason=r.reason, customer_id=r.customer_id,
            )
            for r in result.rows
        ],
    )


async def _owned(
    commitment_id: uuid.UUID, business_id: uuid.UUID, db: DbDep
) -> Commitment:
    commitment = await db.get(Commitment, commitment_id)
    if commitment is None or commitment.business_id != business_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Commitment not found"
        )
    return commitment
