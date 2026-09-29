"""
The commitment ledger's own aggregation queries, factored out of
services/api/routers/ledger.py so a non-HTTP caller can read the same
numbers a business owner sees on their dashboard - first consumer:
shared/ai/context.py's build_owner(), for the owner voice interface's
"who owes me money" answer.

ledger.py keeps calling these too (refactored to delegate rather than
duplicate the query) - one source of truth for what "the ledger position"
means, whether it is read over HTTP or read live on a phone call.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.care.commitment_payments import OUTSTANDING
from shared.db.models import (
    Commitment,
    CommitmentDirection,
    CommitmentKind,
    CommitmentStatus,
    Customer,
    ProductVariant,
    Quotation,
    QuotationItem,
    QuotationStatus,
)


@dataclass(slots=True)
class LedgerTotals:
    owed_to_us_paise: int
    owed_by_us_paise: int
    # Both directions combined - a raw "how many things are late" count.
    # Kept for callers that genuinely want that; anything presenting this
    # as receivables (money coming in) should use the *_they_owe fields
    # below instead - a they_owe promise running late is not the same
    # thing as a we_owe promise running late, and showing one red number
    # for both misleads an owner into thinking they're owed money that
    # may actually be money (or an obligation) they owe out.
    overdue_count: int
    overdue_paise: int
    overdue_they_owe_count: int
    overdue_they_owe_paise: int
    overdue_we_owe_count: int
    overdue_we_owe_paise: int
    open_count: int
    unconfirmed_count: int


async def totals(business_id: uuid.UUID, db: AsyncSession) -> LedgerTotals:
    """
    The position, in one call - identical query shape to
    services/api/routers/ledger.py's own ledger_summary, which now
    delegates here rather than keeping a second copy.
    """
    now = datetime.now(timezone.utc)
    open_only = (
        Commitment.business_id == business_id,
        Commitment.status == CommitmentStatus.open,
    )

    async def _total(direction: CommitmentDirection) -> int:
        result = await db.execute(
            # What is still owed, not what was promised - a half-paid
            # instalment counts for the half that hasn't arrived.
            select(func.coalesce(func.sum(OUTSTANDING), 0)).where(
                *open_only, Commitment.direction == direction
            )
        )
        return int(result.scalar_one())

    overdue = await db.execute(
        select(
            func.count(Commitment.id),
            func.coalesce(func.sum(OUTSTANDING), 0),
        ).where(*open_only, Commitment.due_at < now)
    )
    overdue_count, overdue_paise = overdue.one()

    async def _overdue_by_direction(direction: CommitmentDirection) -> tuple[int, int]:
        result = await db.execute(
            select(
                func.count(Commitment.id),
                func.coalesce(func.sum(OUTSTANDING), 0),
            ).where(*open_only, Commitment.due_at < now, Commitment.direction == direction)
        )
        count, paise = result.one()
        return int(count), int(paise)

    overdue_they_owe_count, overdue_they_owe_paise = await _overdue_by_direction(
        CommitmentDirection.they_owe
    )
    overdue_we_owe_count, overdue_we_owe_paise = await _overdue_by_direction(
        CommitmentDirection.we_owe
    )

    open_count = await db.execute(select(func.count(Commitment.id)).where(*open_only))
    unconfirmed = await db.execute(
        select(func.count(Commitment.id)).where(
            Commitment.business_id == business_id,
            Commitment.status == CommitmentStatus.unconfirmed,
        )
    )

    return LedgerTotals(
        owed_to_us_paise=await _total(CommitmentDirection.they_owe),
        owed_by_us_paise=await _total(CommitmentDirection.we_owe),
        overdue_count=int(overdue_count),
        overdue_paise=int(overdue_paise),
        overdue_they_owe_count=overdue_they_owe_count,
        overdue_they_owe_paise=overdue_they_owe_paise,
        overdue_we_owe_count=overdue_we_owe_count,
        overdue_we_owe_paise=overdue_we_owe_paise,
        open_count=int(open_count.scalar_one()),
        unconfirmed_count=int(unconfirmed.scalar_one()),
    )


@dataclass(slots=True)
class OpenCommitmentRow:
    direction: CommitmentDirection
    description: str
    amount_paise: int | None
    due_at: datetime | None
    customer_name: str | None


async def top_open_commitments(
    business_id: uuid.UUID, db: AsyncSession, *, direction: CommitmentDirection, limit: int = 5,
) -> list[OpenCommitmentRow]:
    """
    The few largest/soonest open commitments in one direction, named by
    customer - what an owner voice query reads out loud rather than the
    full paginated list list_commitments serves the dashboard UI (a
    different shape for a different consumer, not reused here).
    """
    result = await db.execute(
        select(Commitment, Customer.display_name)
        .join(Customer, Customer.id == Commitment.customer_id)
        .where(
            Commitment.business_id == business_id,
            Commitment.status == CommitmentStatus.open,
            Commitment.direction == direction,
        )
        .order_by(Commitment.due_at.asc().nullslast(), Commitment.created_at.desc())
        .limit(limit)
    )
    return [
        OpenCommitmentRow(
            direction=c.direction,
            description=c.description,
            # Outstanding, not promised - this is what gets read out as
            # "X owes you" on the owner's voice line.
            amount_paise=c.outstanding_paise,
            due_at=c.due_at,
            customer_name=name,
        )
        for c, name in result.all()
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Type 1 backlog (docs/new/type-1-backlog.md) - #3 delivery promises,
# #2 advance/proforma tracking, #1 price consistency.
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class DeliveryPromiseRow:
    commitment_id: uuid.UUID
    customer_name: str
    description: str
    due_at: datetime | None
    overdue: bool
    source_quote: str | None


async def open_delivery_promises(
    business_id: uuid.UUID, db: AsyncSession, *, limit: int = 50,
) -> list[DeliveryPromiseRow]:
    """
    What this business promised to deliver, and what's already late - Type
    1 backlog #3 ("nearly free": CommitmentKind.delivery extraction already
    runs, this is a query and a view on top of it). we_owe only - what a
    customer promised to send back is a different question, not this one.
    "Delivery" here covers whatever the conversation actually promised: a
    dispatch, a deliverable, a feature or a fix - one mechanism, three
    vocabularies, per the backlog doc.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Commitment, Customer.display_name)
        .join(Customer, Customer.id == Commitment.customer_id)
        .where(
            Commitment.business_id == business_id,
            Commitment.status == CommitmentStatus.open,
            Commitment.direction == CommitmentDirection.we_owe,
            Commitment.kind == CommitmentKind.delivery,
        )
        .order_by(Commitment.due_at.asc().nullslast(), Commitment.created_at.desc())
        .limit(limit)
    )
    return [
        DeliveryPromiseRow(
            commitment_id=c.id,
            customer_name=name,
            description=c.description,
            due_at=c.due_at,
            overdue=bool(c.due_at and c.due_at < now),
            source_quote=c.source_quote,
        )
        for c, name in result.all()
    ]


@dataclass(slots=True)
class MissingAdvanceRow:
    quotation_id: uuid.UUID
    reference: str | None
    customer_name: str
    total_paise: int | None
    won_at: datetime | None


async def quotations_missing_advance(
    business_id: uuid.UUID, db: AsyncSession, *, limit: int = 50,
) -> list[MissingAdvanceRow]:
    """
    Won quotes with no advance/proforma payment on the Ledger yet - Type 1
    backlog #2, "closes the dead end after won". NOT EXISTS rather than a
    LEFT JOIN + IS NULL: a won quote can have several linked commitments
    (an advance and, later, a balance), and this only wants "zero so far",
    which NOT EXISTS reads as directly as the English sentence does.
    """
    result = await db.execute(
        select(Quotation, Customer.display_name)
        .join(Customer, Customer.id == Quotation.customer_id)
        .where(
            Quotation.business_id == business_id,
            Quotation.status == QuotationStatus.won,
            ~select(Commitment.id)
            .where(Commitment.quotation_id == Quotation.id)
            .exists(),
        )
        .order_by(Quotation.updated_at.desc())
        .limit(limit)
    )
    return [
        MissingAdvanceRow(
            quotation_id=q.id, reference=q.reference, customer_name=name,
            total_paise=q.total_paise, won_at=q.updated_at,
        )
        for q, name in result.all()
    ]


@dataclass(slots=True)
class PriceInconsistencyRow:
    variant_id: uuid.UUID
    variant_title: str | None
    price_tier: str | None
    # (quotation_id, reference, customer_name, unit_price_paise) per quote
    # that quoted this variant to this tier at a different price than the
    # others.
    quotes: list[tuple[uuid.UUID, str | None, str, int]]


async def price_inconsistencies(
    business_id: uuid.UUID, db: AsyncSession,
) -> list[PriceInconsistencyRow]:
    """
    The same SKU quoted at different prices to customers in the same
    pricing tier - Type 1 backlog #1. Grouped by (variant, price_tier), not
    by variant alone: a Dealer-tier price legitimately differs from a
    Distributor-tier one, and without the tier this would cry wolf on
    every business that prices by tier at all (the exact failure the
    backlog doc flagged). A customer with no price_tier set is grouped
    under None - "unclassified" - rather than silently excluded, so an
    owner sees there's tiering to do before the check can say more.

    Read every non-lost, non-expired quote's priced items in one query and
    group in Python - a Type 1 business's quote volume is nowhere near
    where that stops being the simplest correct approach.
    """
    result = await db.execute(
        select(
            QuotationItem.variant_id, QuotationItem.unit_price_paise,
            Quotation.id, Quotation.reference, Customer.display_name, Customer.price_tier,
            ProductVariant.title,
        )
        .join(Quotation, Quotation.id == QuotationItem.quotation_id)
        .join(Customer, Customer.id == Quotation.customer_id)
        .join(ProductVariant, ProductVariant.id == QuotationItem.variant_id)
        .where(
            QuotationItem.business_id == business_id,
            QuotationItem.variant_id.is_not(None),
            QuotationItem.unit_price_paise.is_not(None),
            Quotation.status.notin_([QuotationStatus.lost, QuotationStatus.expired]),
        )
    )

    groups: dict[tuple[uuid.UUID, str | None], list] = {}
    titles: dict[uuid.UUID, str | None] = {}
    for variant_id, price, quote_id, ref, cust_name, tier, title in result.all():
        titles[variant_id] = title
        groups.setdefault((variant_id, tier), []).append((quote_id, ref, cust_name, price))

    out = []
    for (variant_id, tier), quotes in groups.items():
        if len({price for *_, price in quotes}) > 1:
            out.append(
                PriceInconsistencyRow(
                    variant_id=variant_id, variant_title=titles.get(variant_id),
                    price_tier=tier, quotes=quotes,
                )
            )
    return out
