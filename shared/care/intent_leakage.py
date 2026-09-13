"""
D2C risk sweeps - order_sync capability. Two deterministic joins over
already-shipped order data, neither needing a new extraction pass:

check_intent_leakage: a customer who abandoned a checkout (real purchase
intent, browsed to the point of starting to pay) and separately messaged
the business on a conversational channel in the same window has engaged
twice without converting - a signal no single-channel competitor tool can
produce, since none of them resolve one customer across Shopify +
WhatsApp/Instagram/email the way shared/identity/resolver.py already does
for every channel in this codebase. This sweep makes that identity
resolution pay off as a concrete business signal, not just a technical
nicety.

check_rto_risk_pincodes: Order.shipping_pincode (captured off Shopify's
own shipping_address.zip) joined against Order.ndr_at (a real courier
fact, stamped by shared/care/shiprocket_sync.py's own NDR poll) surfaces
a business's own delivery-failure history per pincode - not a courier-
level API KROVA doesn't have, just the business's own past orders it
already stores. Flags, never blocks: a heuristic wrongly delaying a
legitimate order is a worse failure than a missed flag, so the COD
confirmation send this pairs with still goes out unblocked either way.

Both use the same dedupe/title-fingerprint shape as shared/ai/
recall_insights.py's own sweep - one Insight per customer/pincode, never
duplicated on a rerun.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared import verticals
from shared.care.signal_dispatch import dispatch_signal
from shared.db.models import AbandonedCheckout, Business, Customer, Message, Order
from shared.db.models.intelligence import Insight
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# How far back an abandoned checkout still counts as a live signal - wide
# enough to catch a customer who circles back to ask a question days
# later, narrow enough that this stays "recent," not a permanent flag.
_LOOKBACK = timedelta(days=7)

# How many of a business's own prior orders in the same pincode must have
# hit NDR before a new COD order there is worth flagging - two is enough
# to be a real pattern, not one bad delivery attempt.
_RTO_RISK_THRESHOLD = 2


async def _existing_titles(business_id, db: AsyncSession) -> set[str]:
    result = await db.execute(select(Insight.title).where(Insight.business_id == business_id))
    return {(t or "").strip().lower() for t in result.scalars().all()}


def _title_for(customer: Customer) -> str:
    name = customer.display_name or "A customer"
    return f"Intent leakage: {name} engaged but didn't check out"


async def check_intent_leakage(db: AsyncSession) -> int:
    """Scan for customers with real cross-channel engagement and an
    unconverted checkout. Returns how many Insight rows created."""
    now = datetime.now(timezone.utc)
    cutoff = now - _LOOKBACK

    result = await db.execute(
        select(Business).where(Business.is_active.is_(True))
    )
    businesses = [b for b in result.scalars().all() if verticals.has_capability(b.vertical, "order_sync")]
    if not businesses:
        return 0

    created = 0
    for business in businesses:
        existing = await _existing_titles(business.id, db)

        checkouts = await db.execute(
            select(AbandonedCheckout).where(
                AbandonedCheckout.business_id == business.id,
                AbandonedCheckout.recovered_at.is_(None),
                AbandonedCheckout.customer_id.is_not(None),
                AbandonedCheckout.abandoned_at >= cutoff,
            )
        )
        for checkout in checkouts.scalars().all():
            # Real conversational engagement in the same window - any
            # channel, inbound or outbound, is enough to prove this
            # customer is a real, active conversation, not just a
            # browser who never spoke to the business at all. Cited by
            # id below, same provenance rule every other derived fact in
            # this schema already follows (see this module's own
            # docstring).
            message_ids = (
                await db.execute(
                    select(Message.id)
                    .where(
                        Message.business_id == business.id,
                        Message.customer_id == checkout.customer_id,
                        Message.occurred_at >= checkout.abandoned_at - _LOOKBACK,
                    )
                    .order_by(Message.occurred_at.desc())
                    .limit(5)
                )
            ).scalars().all()
            if not message_ids:
                continue

            customer = await db.get(Customer, checkout.customer_id)
            if customer is None:
                continue

            title = _title_for(customer)
            if title.strip().lower() in existing:
                continue

            body = (
                (
                    f"Started checking out (₹{checkout.total_paise / 100:,.0f}) "
                    if checkout.total_paise else "Started checking out "
                ) + "and messaged the business separately, but never completed the order."
            )
            db.add(
                Insight(
                    business_id=business.id,
                    customer_id=customer.id,
                    kind="intent_leakage",
                    title=title,
                    body=body,
                    severity="info",
                    source_message_ids=list(message_ids),
                    created_at=now,
                )
            )
            await dispatch_signal(
                db, business_id=business.id, customer_id=customer.id, channel=None,
                kind="intent_leakage", title=title, body=body, severity="info",
            )
            existing.add(title.strip().lower())
            created += 1

    if created:
        logger.info("created %s intent-leakage insight(s)", created)
    return created


def _rto_title_for(pincode: str) -> str:
    return f"Delivery risk in {pincode}"


async def check_rto_risk_pincodes(db: AsyncSession) -> int:
    """
    For every new COD order not yet checked, count this business's own
    prior orders sharing a shipping_pincode where ndr_at is set. At or
    past _RTO_RISK_THRESHOLD, raise an Insight citing those prior orders -
    an actionable heads-up for staff to verify by phone before shipping,
    not a block on the order itself. See this module's own docstring.
    """
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(Business).where(Business.is_active.is_(True))
    )
    businesses = [b for b in result.scalars().all() if verticals.has_capability(b.vertical, "order_sync")]
    if not businesses:
        return 0

    created = 0
    for business in businesses:
        existing = await _existing_titles(business.id, db)

        candidates = await db.execute(
            select(Order).where(
                Order.business_id == business.id,
                Order.is_cod.is_(True),
                Order.shipping_pincode.is_not(None),
                Order.rto_risk_checked_at.is_(None),
            )
        )
        for order in candidates.scalars().all():
            # Stamped regardless of outcome below - same "never
            # reprocess" discipline as every other sweep dedupe column.
            order.rto_risk_checked_at = now

            prior = (
                await db.execute(
                    select(Order.id).where(
                        Order.business_id == business.id,
                        Order.shipping_pincode == order.shipping_pincode,
                        Order.ndr_at.is_not(None),
                        Order.id != order.id,
                    )
                )
            ).scalars().all()
            if len(prior) < _RTO_RISK_THRESHOLD:
                continue

            title = _rto_title_for(order.shipping_pincode)
            if title.strip().lower() in existing:
                continue

            body = (
                f"{len(prior)} prior orders shipped to pincode {order.shipping_pincode} "
                "were not delivered (NDR) - consider a verification call before shipping "
                f"order #{order.order_number or order.id}."
            )
            db.add(
                Insight(
                    business_id=business.id,
                    customer_id=order.customer_id,
                    kind="rto_risk",
                    title=title,
                    body=body,
                    severity="warning",
                    source_message_ids=[],
                    created_at=now,
                )
            )
            await dispatch_signal(
                db, business_id=business.id, customer_id=order.customer_id, channel=None,
                kind="rto_risk", title=title, body=body, severity="warning",
            )
            existing.add(title.strip().lower())
            created += 1

    if created:
        logger.info("created %s RTO-risk insight(s)", created)
    return created
