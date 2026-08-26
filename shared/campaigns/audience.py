"""
Working out who a campaign should reach.

The audience is a question about the ledger, so this is where "everyone who
owes me money" becomes a list of people with their own figures attached.

Two things happen here that a CSV upload cannot do.

Each recipient carries their own data. A payment reminder to Priya says
₹4,500 due on the 28th because that is what her conversation contained, not
because someone typed it into a spreadsheet column. The variables are
resolved per person from their own commitments.

Nobody is included who should not be. Private customers are excluded, people
with no reachable number are excluded and counted separately, and the daily
tier limit is respected rather than discovered halfway through a send.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    Audience,
    Commitment,
    CommitmentDirection,
    CommitmentStatus,
    Customer,
    CustomerIdentity,
    IdentityKind,
)


@dataclass(slots=True)
class Recipient:
    customer_id: uuid.UUID
    name: str | None
    phone: str
    # What this person's message should say, keyed by the field names a
    # campaign maps onto template variables.
    values: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class AudienceResult:
    recipients: list[Recipient]
    skipped: list[dict]
    total_amount_paise: int

    @property
    def count(self) -> int:
        return len(self.recipients)


def _money(paise: int | None) -> str:
    return f"₹{(paise or 0) / 100:,.0f}"


async def resolve(
    business_id: uuid.UUID,
    audience: Audience,
    params: dict,
    db: AsyncSession,
    *,
    limit: int = 1000,
) -> AudienceResult:
    """
    Turn an audience question into people, with their own figures attached.
    """
    now = datetime.now(timezone.utc)
    skipped: list[dict] = []

    # Start from customers, never from commitments - the same person can have
    # three open promises and must appear once, not three times.
    conditions = [
        Customer.business_id == business_id,
        Customer.is_private == False,  # noqa: E712
    ]

    commitment_filter = None
    if audience == Audience.owes_money:
        commitment_filter = (
            Commitment.direction == CommitmentDirection.they_owe,
            Commitment.status == CommitmentStatus.open,
        )
    elif audience == Audience.overdue:
        commitment_filter = (
            Commitment.direction == CommitmentDirection.they_owe,
            Commitment.status == CommitmentStatus.open,
            Commitment.due_at < now,
        )
    elif audience == Audience.we_promised:
        commitment_filter = (
            Commitment.direction == CommitmentDirection.we_owe,
            Commitment.status == CommitmentStatus.open,
        )
    elif audience == Audience.gone_quiet:
        days = int(params.get("days", 30))
        conditions.append(Customer.last_contact_at < now - timedelta(days=days))

    if commitment_filter is not None:
        matching = select(Commitment.customer_id).where(
            Commitment.business_id == business_id, *commitment_filter
        )
        min_paise = params.get("min_amount_paise")
        if min_paise:
            matching = matching.where(Commitment.amount_paise >= int(min_paise))
        conditions.append(Customer.id.in_(matching))

    rows = await db.execute(
        select(Customer)
        .where(*conditions)
        .order_by(Customer.last_contact_at.desc().nullslast())
        .limit(limit)
    )
    customers = list(rows.scalars().all())
    if not customers:
        return AudienceResult(recipients=[], skipped=[], total_amount_paise=0)

    ids = [c.id for c in customers]

    # Phone numbers in one query. WhatsApp needs one; anyone without is
    # skipped visibly rather than silently dropped.
    phones = dict(
        (
            await db.execute(
                select(CustomerIdentity.customer_id, CustomerIdentity.value).where(
                    CustomerIdentity.customer_id.in_(ids),
                    CustomerIdentity.kind == IdentityKind.phone,
                )
            )
        ).all()
    )

    # Their open commitments, so each message carries their own figures.
    relevant_direction = (
        CommitmentDirection.we_owe
        if audience == Audience.we_promised
        else CommitmentDirection.they_owe
    )
    commitments = (
        await db.execute(
            select(Commitment)
            .where(
                Commitment.customer_id.in_(ids),
                Commitment.status == CommitmentStatus.open,
                Commitment.direction == relevant_direction,
            )
            .order_by(Commitment.due_at.asc().nullslast())
        )
    ).scalars().all()

    by_customer: dict[uuid.UUID, list[Commitment]] = {}
    for c in commitments:
        by_customer.setdefault(c.customer_id, []).append(c)

    recipients: list[Recipient] = []
    total = 0

    for customer in customers:
        phone = phones.get(customer.id)
        if not phone:
            skipped.append(
                {
                    "customer_id": str(customer.id),
                    "name": customer.display_name,
                    "reason": "No phone number on file",
                }
            )
            continue

        theirs = by_customer.get(customer.id, [])
        owed = sum(c.amount_paise or 0 for c in theirs)
        total += owed
        soonest = next((c for c in theirs if c.due_at), theirs[0] if theirs else None)

        recipients.append(
            Recipient(
                customer_id=customer.id,
                name=customer.display_name,
                phone=phone,
                values={
                    # Never leave a template variable empty - Meta sends the
                    # literal placeholder and the customer sees {{1}}.
                    "customer_name": customer.display_name or "there",
                    "amount": _money(owed) if owed else "the outstanding amount",
                    "due_date": (
                        soonest.due_at.strftime("%d %B")
                        if soonest and soonest.due_at
                        else "shortly"
                    ),
                    "description": soonest.description if soonest else "",
                    "count": str(len(theirs)),
                },
            )
        )

    return AudienceResult(
        recipients=recipients, skipped=skipped, total_amount_paise=total
    )


async def sent_today(business_id: uuid.UUID, db: AsyncSession) -> int:
    """
    How many distinct people this business has messaged today.

    Meta's limit counts unique recipients per rolling day, so a campaign has
    to know what has already been used before it starts - discovering the
    ceiling halfway through a send leaves half an audience wondering why they
    were left out.
    """
    from shared.db.models import Direction, Message

    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(func.count(func.distinct(Message.customer_id))).where(
            Message.business_id == business_id,
            Message.direction == Direction.outbound,
            Message.occurred_at >= start,
        )
    )
    return int(result.scalar_one())
