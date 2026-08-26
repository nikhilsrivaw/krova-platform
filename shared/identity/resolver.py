"""
Working out which human a message came from.

This is the foundation the whole multi-channel idea rests on. Without it, four
connected channels are four separate inboxes and Krova is a worse version of
what everyone else sells. With it, someone who WhatsApps on Monday and phones
on Wednesday is one customer with one history - and the agent can open the
call already knowing about Monday.

The free win is worth stating plainly: WhatsApp and voice both identify a
person by phone number. Normalise both to the same form and those two channels
unify with no matching logic at all. Email and Instagram are the hard ones,
and they are where confidence scores matter.

Rule throughout: a wrong merge is much worse than a missed one. Showing one
customer another customer's history is unrecoverable; leaving two records
unlinked is merely untidy, and fixable later.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Customer, CustomerIdentity, IdentityKind
from shared.identity.normalise import InvalidIdentifier, normalise
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Below this, an identity is recorded but never used to merge two customers
# on its own. It waits for a human to confirm.
AUTO_LINK_CONFIDENCE = 0.9


@dataclass(slots=True)
class Resolution:
    customer: Customer
    created: bool
    identity: CustomerIdentity


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def resolve(
    business_id: uuid.UUID,
    kind: IdentityKind | str,
    raw_value: str,
    db: AsyncSession,
    *,
    display_name: str | None = None,
    confidence: float = 1.0,
    verified: bool = True,
) -> Resolution:
    """
    Find or create the customer behind an identifier.

    Called on every inbound message, so it is one indexed lookup in the common
    case - the unique index on (business_id, kind, value) is what keeps it
    cheap enough to sit on the hot path.
    """
    kind_value = kind.value if isinstance(kind, IdentityKind) else str(kind)
    value = normalise(kind_value, raw_value)

    existing = await _find_identity(business_id, kind_value, value, db)
    if existing is not None:
        customer = await db.get(Customer, existing.customer_id)
        if customer is None:
            # The identity outlived its customer. Treat it as absent rather
            # than returning None into the caller's happy path.
            logger.error("orphaned identity %s -> missing customer", existing.id)
            await db.delete(existing)
        else:
            if display_name and not customer.display_name:
                customer.display_name = display_name
            customer.last_contact_at = _now()
            return Resolution(customer=customer, created=False, identity=existing)

    customer = Customer(
        business_id=business_id,
        display_name=display_name,
        first_seen_at=_now(),
        last_contact_at=_now(),
    )
    db.add(customer)
    await db.flush()

    identity = CustomerIdentity(
        business_id=business_id,
        customer_id=customer.id,
        kind=kind_value,
        value=value,
        confidence=confidence,
        verified=verified,
        created_at=_now(),
    )
    db.add(identity)

    try:
        await db.flush()
    except IntegrityError:
        # Two messages from the same new customer arriving together. The
        # unique index did its job; take whichever row won.
        await db.rollback()
        winner = await _find_identity(business_id, kind_value, value, db)
        if winner is None:
            raise
        existing_customer = await db.get(Customer, winner.customer_id)
        if existing_customer is None:
            raise
        logger.info("identity race resolved for %s:%s", kind_value, value)
        return Resolution(customer=existing_customer, created=False, identity=winner)

    logger.info(
        "new customer %s for business %s via %s", customer.id, business_id, kind_value
    )
    return Resolution(customer=customer, created=True, identity=identity)


async def link_identity(
    customer_id: uuid.UUID,
    business_id: uuid.UUID,
    kind: IdentityKind | str,
    raw_value: str,
    db: AsyncSession,
    *,
    confidence: float = 1.0,
    verified: bool = False,
    notes: str | None = None,
) -> CustomerIdentity | None:
    """
    Attach another handle to a known customer.

    This is how channels join up: an email address mentioned in a WhatsApp
    conversation, a phone number given on a call. Returns None when the
    identifier is unusable or already belongs to somebody else.

    It will not move an identity between customers. If this handle is already
    attached elsewhere, that is a conflict for a human to look at - silently
    reassigning it would merge two people's histories on a guess.
    """
    kind_value = kind.value if isinstance(kind, IdentityKind) else str(kind)
    try:
        value = normalise(kind_value, raw_value)
    except InvalidIdentifier as exc:
        logger.debug("not linking unusable identifier: %s", exc)
        return None

    existing = await _find_identity(business_id, kind_value, value, db)
    if existing is not None:
        if existing.customer_id != customer_id:
            logger.warning(
                "identity %s:%s already belongs to customer %s, not linking to %s",
                kind_value,
                value,
                existing.customer_id,
                customer_id,
            )
            return None
        # Already ours. Keep the stronger claim.
        if confidence > existing.confidence:
            existing.confidence = confidence
        if verified:
            existing.verified = True
        return existing

    identity = CustomerIdentity(
        business_id=business_id,
        customer_id=customer_id,
        kind=kind_value,
        value=value,
        confidence=confidence,
        verified=verified,
        created_at=_now(),
        notes=notes,
    )
    db.add(identity)
    await db.flush()
    return identity


async def identities_for(
    customer_id: uuid.UUID, db: AsyncSession
) -> list[CustomerIdentity]:
    result = await db.execute(
        select(CustomerIdentity).where(CustomerIdentity.customer_id == customer_id)
    )
    return list(result.scalars().all())


async def merge_customers(
    keep_id: uuid.UUID, absorb_id: uuid.UUID, db: AsyncSession
) -> None:
    """
    Fold one customer into another.

    Only ever called with a human's confirmation, or on an exact match the
    system can prove. Everything the absorbed customer owns moves across, and
    the empty shell is deleted so nothing points at a record that no longer
    means anything.
    """
    if keep_id == absorb_id:
        return

    from shared.db.models import Call, Commitment, Message

    for model in (Message, Commitment, Call):
        rows = await db.execute(select(model).where(model.customer_id == absorb_id))
        for row in rows.scalars().all():
            row.customer_id = keep_id

    for identity in await identities_for(absorb_id, db):
        identity.customer_id = keep_id

    absorbed = await db.get(Customer, absorb_id)
    keeper = await db.get(Customer, keep_id)
    if absorbed is not None and keeper is not None:
        if not keeper.display_name and absorbed.display_name:
            keeper.display_name = absorbed.display_name
        if absorbed.first_seen_at and (
            keeper.first_seen_at is None or absorbed.first_seen_at < keeper.first_seen_at
        ):
            keeper.first_seen_at = absorbed.first_seen_at
        # A customer marked private stays private. Merging must never quietly
        # bring a protected conversation back into the agent's reach.
        if absorbed.is_private:
            keeper.is_private = True
        await db.delete(absorbed)

    await db.flush()
    logger.info("merged customer %s into %s", absorb_id, keep_id)


async def _find_identity(
    business_id: uuid.UUID, kind: str, value: str, db: AsyncSession
) -> CustomerIdentity | None:
    result = await db.execute(
        select(CustomerIdentity).where(
            CustomerIdentity.business_id == business_id,
            CustomerIdentity.kind == kind,
            CustomerIdentity.value == value,
        )
    )
    return result.scalar_one_or_none()
