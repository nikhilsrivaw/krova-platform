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

from shared.db.models import Commitment, CommitmentDirection, CommitmentStatus, Customer


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

    async def _overdue_by_direction(direction: CommitmentDirection) -> tuple[int, int]:
        result = await db.execute(
            select(
                func.count(Commitment.id),
                func.coalesce(func.sum(Commitment.amount_paise), 0),
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
            amount_paise=c.amount_paise,
            due_at=c.due_at,
            customer_name=name,
        )
        for c, name in result.all()
    ]
