"""
Seeding a customer list from outside a conversation.

Until this, every Customer row came from an inbound message - identity.
resolver.resolve() stamps last_contact_at because a real message just
arrived, and that field is exactly what the customers list sorts by
(nullslast, so an unimported contact would sort last) and what the
`gone_quiet` campaign audience measures against. An imported contact has
never actually written in, so last_contact_at must stay None here - setting
it to "now" would make a business's own spreadsheet lie about who has
actually been in touch.

Runs as many independent row-level transactions as rows, not one big one.
A CSV of a few thousand rows genuinely can hit a same-moment race with a
real inbound webhook creating the same identity concurrently - unlikely for
any one row, not unlikely across a few thousand - and the request as a
whole must not roll back every row already committed because row 4,000 hit
that race. Each row commits via its own SAVEPOINT rather than sharing the
request's one outer transaction, which is what makes "3,999 succeeded, one
didn't" a true statement rather than a lost commit.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Customer, CustomerIdentity, IdentityKind
from shared.identity.normalise import InvalidIdentifier, normalise_phone
from shared.utils.logging import get_logger

logger = get_logger(__name__)

MAX_ROWS_PER_IMPORT = 5000


@dataclass(slots=True)
class ImportRow:
    row_number: int
    phone: str
    name: str | None = None


@dataclass(slots=True)
class RowResult:
    row_number: int
    phone: str
    outcome: str  # "created" | "already_existed" | "invalid"
    reason: str | None = None
    customer_id: str | None = None


@dataclass(slots=True)
class ImportResult:
    created: int = 0
    already_existed: int = 0
    invalid: int = 0
    rows: list[RowResult] = field(default_factory=list)


async def _find_identity(
    business_id: uuid.UUID, value: str, db: AsyncSession
) -> CustomerIdentity | None:
    result = await db.execute(
        select(CustomerIdentity).where(
            CustomerIdentity.business_id == business_id,
            CustomerIdentity.kind == IdentityKind.phone,
            CustomerIdentity.value == value,
        )
    )
    return result.scalar_one_or_none()


async def import_contacts(
    business_id: uuid.UUID, rows: list[ImportRow], db: AsyncSession
) -> ImportResult:
    result = ImportResult()

    for row in rows[:MAX_ROWS_PER_IMPORT]:
        try:
            value = normalise_phone(row.phone)
        except InvalidIdentifier as exc:
            result.invalid += 1
            result.rows.append(RowResult(row.row_number, row.phone, "invalid", str(exc)))
            continue

        found = await _find_identity(business_id, value, db)
        if found is not None:
            customer = await db.get(Customer, found.customer_id)
            # A name from the sheet fills a gap; it never overwrites one a
            # real conversation already gave the customer.
            if customer is not None and row.name and not customer.display_name:
                customer.display_name = row.name
            result.already_existed += 1
            result.rows.append(
                RowResult(row.row_number, row.phone, "already_existed", customer_id=str(found.customer_id))
            )
            continue

        try:
            async with db.begin_nested():
                customer = Customer(
                    business_id=business_id,
                    display_name=row.name,
                    first_seen_at=datetime.now(timezone.utc),
                    last_contact_at=None,
                )
                db.add(customer)
                await db.flush()

                db.add(CustomerIdentity(
                    business_id=business_id,
                    customer_id=customer.id,
                    kind=IdentityKind.phone,
                    value=value,
                    confidence=1.0,
                    # Nobody has proven this number belongs to this person by
                    # actually messaging from it - it came from a spreadsheet.
                    verified=False,
                    created_at=datetime.now(timezone.utc),
                ))
                await db.flush()
        except IntegrityError:
            # A real inbound message created this same identity in the
            # instant between the check above and this insert. Their real
            # identity wins; look it up and report it, rather than erroring.
            winner = await _find_identity(business_id, value, db)
            result.already_existed += 1
            result.rows.append(
                RowResult(
                    row.row_number, row.phone, "already_existed",
                    customer_id=str(winner.customer_id) if winner else None,
                )
            )
            continue

        result.created += 1
        result.rows.append(RowResult(row.row_number, row.phone, "created", customer_id=str(customer.id)))

    logger.info(
        "contact import business=%s created=%s existed=%s invalid=%s",
        business_id, result.created, result.already_existed, result.invalid,
    )
    return result
