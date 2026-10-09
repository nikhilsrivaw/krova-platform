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
    CustomerTag,
    IdentityKind,
    TagStatus,
)
from shared.identity import resolver as identity_resolver
from shared.identity.normalise import InvalidIdentifier, normalise_phone

# A hand-typed list is for a test or a handful of people; a real audience
# comes from the ledger, not from pasting a spreadsheet into a text box.
MAX_MANUAL_NUMBERS = 25


async def _customers_for_numbers(
    business_id: uuid.UUID, raw_numbers: list, db: AsyncSession, *, create_missing: bool = True
) -> tuple[list[uuid.UUID], list[str], list[dict]]:
    """
    (customer ids, numbers with no customer yet, skip entries) for hand-typed
    numbers - one skip entry for each that is unusable.

    A number that is not yet a customer becomes a bare one when
    create_missing is set - the campaign needs a customer to attach its
    recipient row to, and the person typing it in is the one vouching it is
    real. A preview passes create_missing=False and gets those numbers back
    in the second list instead: a preview re-runs on every keystroke, and
    creating a customer for each half-typed number would litter the CRM.

    An existing customer is looked up, not resolved: identity_resolver.resolve
    stamps last_contact_at, which would quietly take a real customer out of a
    "gone quiet" audience just because someone sent them a test.
    """
    skipped: list[dict] = []
    normalised: list[str] = []
    for raw in raw_numbers[:MAX_MANUAL_NUMBERS]:
        text = str(raw).strip()
        if not text:
            continue
        try:
            number = normalise_phone(text)
        except InvalidIdentifier:
            skipped.append({"customer_id": None, "name": text, "reason": "Not a valid phone number"})
            continue
        if number not in normalised:
            normalised.append(number)

    if not normalised:
        return [], [], skipped

    found = dict(
        (
            await db.execute(
                select(CustomerIdentity.value, CustomerIdentity.customer_id).where(
                    CustomerIdentity.business_id == business_id,
                    CustomerIdentity.kind == IdentityKind.phone,
                    CustomerIdentity.value.in_(normalised),
                )
            )
        ).all()
    )

    ids: list[uuid.UUID] = []
    new_numbers: list[str] = []
    for number in normalised:
        customer_id = found.get(number)
        if customer_id is None:
            if not create_missing:
                new_numbers.append(number)
                continue
            resolution = await identity_resolver.resolve(
                business_id, IdentityKind.phone, number, db
            )
            customer_id = resolution.customer.id
        ids.append(customer_id)
    return ids, new_numbers, skipped


@dataclass(slots=True)
class Recipient:
    # None only for a hand-typed number in a preview that is not yet a
    # customer - a real send always creates the customer first.
    customer_id: uuid.UUID | None
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
    # True for a MARKETING-category template - WhatsApp policy (and
    # India's own DPDPA) requires documented consent before one of those
    # reaches a number, which ordinary conversation does not establish.
    # UTILITY/AUTHENTICATION sends leave this false - a transactional
    # message like a payment reminder isn't "marketing".
    require_marketing_opt_in: bool = False,
    # Only matters for Audience.numbers. False for a preview, so previewing a
    # typed number never writes a customer - see _customers_for_numbers.
    create_missing: bool = True,
) -> AudienceResult:
    """
    Turn an audience question into people, with their own figures attached.
    """
    now = datetime.now(timezone.utc)
    skipped: list[dict] = []
    # Typed-in numbers that are not customers yet (preview only).
    new_recipients: list[Recipient] = []

    # Start from customers, never from commitments - the same person can have
    # three open promises and must appear once, not three times.
    conditions = [Customer.business_id == business_id]
    # Hand-typed numbers are the one audience where a private customer is
    # not silently dropped by the query - it is skipped below with a reason,
    # so the person who typed the number can see why nothing went to it.
    if audience != Audience.numbers:
        conditions.append(Customer.is_private == False)  # noqa: E712

    commitment_filter = None
    if audience == Audience.numbers:
        manual_ids, new_numbers, invalid = await _customers_for_numbers(
            business_id, list(params.get("numbers") or []), db, create_missing=create_missing
        )
        skipped.extend(invalid)
        new_recipients = [
            Recipient(
                customer_id=None,
                name=None,
                phone=number,
                values={
                    "customer_name": "there",
                    "amount": "the outstanding amount",
                    "due_date": "shortly",
                    "description": "",
                    "count": "0",
                },
            )
            for number in new_numbers
        ]
        if not manual_ids:
            return AudienceResult(recipients=new_recipients, skipped=skipped, total_amount_paise=0)
        conditions.append(Customer.id.in_(manual_ids))
    elif audience == Audience.owes_money:
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
    elif audience == Audience.by_tag:
        label = (params.get("tag") or "").strip().lower()
        if not label:
            return AudienceResult(recipients=[], skipped=[], total_amount_paise=0)
        tagged = select(CustomerTag.customer_id).where(
            CustomerTag.business_id == business_id,
            CustomerTag.label == label,
            CustomerTag.status == TagStatus.confirmed,
        )
        conditions.append(Customer.id.in_(tagged))

    if commitment_filter is not None:
        matching = select(Commitment.customer_id).where(
            Commitment.business_id == business_id, *commitment_filter
        )
        min_paise = params.get("min_amount_paise")
        if min_paise:
            # Still owed, not originally promised - a customer who has
            # paid most of it shouldn't land in a "owes over ₹10,000" chase.
            from shared.care.commitment_payments import OUTSTANDING

            matching = matching.where(OUTSTANDING >= int(min_paise))
        conditions.append(Customer.id.in_(matching))

    rows = await db.execute(
        select(Customer)
        .where(*conditions)
        .order_by(Customer.last_contact_at.desc().nullslast())
        .limit(limit)
    )
    customers = list(rows.scalars().all())
    if not customers:
        return AudienceResult(recipients=new_recipients, skipped=skipped, total_amount_paise=0)

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

        if audience == Audience.numbers and customer.is_private:
            skipped.append(
                {
                    "customer_id": str(customer.id),
                    "name": customer.display_name,
                    "reason": "Marked private - not messaged",
                }
            )
            continue

        # A number typed in by hand is a test or a one-off the person has
        # chosen deliberately, so it is not held to the marketing opt-in
        # list that protects a whole-audience send.
        if (
            require_marketing_opt_in
            and audience != Audience.numbers
            and not customer.marketing_opt_in
        ):
            skipped.append(
                {
                    "customer_id": str(customer.id),
                    "name": customer.display_name,
                    "reason": "Not opted in to marketing messages",
                }
            )
            continue

        theirs = by_customer.get(customer.id, [])
        owed = sum(c.outstanding_paise or 0 for c in theirs)
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

    recipients.extend(new_recipients)
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
