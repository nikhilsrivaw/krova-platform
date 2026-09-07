"""
The chronic-care recall sweep - care_recall capability.

Same shape as shared/scheduling/reminders.py's appointment-reminder sweep: a
time-window scan, not a scheduled-per-commitment job, self-limiting the same
way (a commitment whose window passed without a successful send is simply
picked up again next run until it sends, then reminder_sent_at stops it
matching).

Sends unconditionally, independent of Business.autonomy - safe specifically
because the message is a fixed, Meta-approved template with no clinical
content (see clinic.json's policy and shared/scheduling/notify.py's
send_recall_reminder docstring), the same category of proactive send
appointment reminders already are. This is not the reply-drafting path, so
the "escalate clinical content regardless of autonomy" rule does not apply
here - there is no clinical content to escalate.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    AbandonedCheckout,
    Appointment,
    AppointmentStatus,
    Business,
    Commitment,
    CommitmentKind,
    CommitmentStatus,
    Customer,
    Order,
    OrderStatus,
    QueueEntry,
    QueueStatus,
)
from shared import verticals
from shared.scheduling import notify
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Same reasoning as reminders.py's _WINDOW: wide enough that a 30-minute poll
# cycle can never fully skip a due commitment even if one run is late.
_WINDOW = timedelta(hours=1)

# How long after a visit "counts" as recently completed - wide enough that
# a 30-minute poll cycle never fully skips one, narrow enough that a review
# request still arrives while the visit is fresh in the customer's mind.
_REVIEW_WINDOW = timedelta(hours=4)

# Cart-recovery window: the standard "still fresh" range found in
# research - a nudge sent within 1-3 hours converts meaningfully better
# than a day-later one, and by the time it's this old the customer has
# usually either bought elsewhere or genuinely isn't coming back.
_CART_RECOVERY_MIN_AGE = timedelta(hours=1)
_CART_RECOVERY_MAX_AGE = timedelta(hours=3)

# Repeat-purchase window - long enough after delivery that "come back"
# doesn't read as premature, short enough that the brand is still fresh.
_REPEAT_PURCHASE_MIN_DAYS = timedelta(days=14)
_REPEAT_PURCHASE_MAX_DAYS = timedelta(days=21)


async def send_due_recalls(db: AsyncSession) -> int:
    """Send every chronic-care recall reminder currently due. Returns how many actually sent."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Commitment).where(
            Commitment.kind == CommitmentKind.meeting,
            Commitment.status == CommitmentStatus.open,
            Commitment.reminder_sent_at.is_(None),
            Commitment.due_at.is_not(None),
            Commitment.due_at >= now - _WINDOW,
            Commitment.due_at <= now + _WINDOW,
        )
    )
    due = list(result.scalars().all())
    if not due:
        return 0

    sent = 0
    for commitment in due:
        business = await db.get(Business, commitment.business_id)
        if business is None or not verticals.has_capability(business.vertical, "care_recall"):
            continue
        customer = await db.get(Customer, commitment.customer_id)
        if customer is None:
            continue

        ok = await notify.send_recall_reminder(db, business=business, customer=customer)
        if ok:
            commitment.reminder_sent_at = now
            sent += 1

    return sent


async def send_review_requests(db: AsyncSession) -> int:
    """
    Two trigger paths, matching what each vertical's data model actually
    sets - see this feature's own explore notes: AppointmentStatus.visited
    is defined but never set anywhere in this codebase today, so a
    scheduling-only business is swept by time (starts_at passed, still
    confirmed) rather than a status transition that doesn't happen in
    practice. QueueStatus.done IS a real, actively-set signal, so an
    opd_queue business is swept by that status directly.

    Gated per-business on Business.settings["google_review_url"] being
    set - opt-in, not a default every business suddenly texts customers
    about.
    """
    now = datetime.now(timezone.utc)
    sent = 0

    queue_result = await db.execute(
        select(QueueEntry).where(
            QueueEntry.status == QueueStatus.done,
            QueueEntry.review_requested_at.is_(None),
            QueueEntry.completed_at.is_not(None),
            QueueEntry.completed_at >= now - _REVIEW_WINDOW,
        )
    )
    appt_result = await db.execute(
        select(Appointment).where(
            Appointment.status == AppointmentStatus.confirmed,
            Appointment.review_requested_at.is_(None),
            Appointment.starts_at <= now,
            Appointment.starts_at >= now - _REVIEW_WINDOW,
        )
    )

    for entry in list(queue_result.scalars().all()):
        # Stamped even when skipped below (no review URL configured, no
        # customer) - otherwise a business with no URL set would leave
        # every past visit permanently reprocessed on every sweep forever.
        entry.review_requested_at = now
        business = await db.get(Business, entry.business_id)
        review_url = (business.settings or {}).get("google_review_url") if business else None
        if business is None or not review_url or entry.customer_id is None:
            continue
        customer = await db.get(Customer, entry.customer_id)
        if customer is None:
            continue
        if await notify.send_review_request(db, business=business, customer=customer, review_url=review_url):
            sent += 1

    for appointment in list(appt_result.scalars().all()):
        appointment.review_requested_at = now  # stamped regardless - see below
        business = await db.get(Business, appointment.business_id)
        review_url = (business.settings or {}).get("google_review_url") if business else None
        if business is None or not review_url:
            continue
        customer = await db.get(Customer, appointment.customer_id)
        if customer is None:
            continue
        if await notify.send_review_request(db, business=business, customer=customer, review_url=review_url):
            sent += 1

    return sent


async def send_cod_confirmations(db: AsyncSession) -> int:
    """
    order_sync capability. Sweeps pending COD orders that haven't been
    asked to confirm yet - a scheduler sweep, not an inline send from the
    Shopify webhook handler, for the same dedupe-and-retry reasoning
    every other proactive send in this module already uses (a business's
    webhook can arrive, then the send itself can fail transiently -
    cod_confirmation_sent_at is what stops it firing twice, same as
    review_requested_at).
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Order).where(
            Order.is_cod.is_(True),
            Order.status == OrderStatus.pending,
            Order.cod_confirmation_sent_at.is_(None),
            Order.cod_confirmed_at.is_(None),
            Order.cod_declined_at.is_(None),
        )
    )
    due = list(result.scalars().all())
    if not due:
        return 0

    sent = 0
    for order in due:
        order.cod_confirmation_sent_at = now  # stamped regardless - see review-request precedent above
        business = await db.get(Business, order.business_id)
        if business is None or not verticals.has_capability(business.vertical, "order_sync"):
            continue
        if order.customer_id is None:
            continue
        customer = await db.get(Customer, order.customer_id)
        if customer is None:
            continue
        if await notify.send_cod_confirmation(
            db, business=business, customer=customer,
            order_number=order.order_number or str(order.id), total_paise=order.total_paise,
        ):
            sent += 1

    return sent


async def send_abandoned_cart_recovery(db: AsyncSession) -> int:
    """order_sync capability. See _CART_RECOVERY_MIN_AGE/_MAX_AGE for the window reasoning."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(AbandonedCheckout).where(
            AbandonedCheckout.recovery_sent_at.is_(None),
            AbandonedCheckout.recovered_at.is_(None),
            AbandonedCheckout.abandoned_at <= now - _CART_RECOVERY_MIN_AGE,
            AbandonedCheckout.abandoned_at >= now - _CART_RECOVERY_MAX_AGE,
        )
    )
    due = list(result.scalars().all())
    if not due:
        return 0

    sent = 0
    for checkout in due:
        checkout.recovery_sent_at = now  # stamped regardless - see review-request precedent above
        business = await db.get(Business, checkout.business_id)
        if business is None or not verticals.has_capability(business.vertical, "order_sync"):
            continue
        if checkout.customer_id is None or not checkout.checkout_url:
            continue
        customer = await db.get(Customer, checkout.customer_id)
        if customer is None:
            continue
        if await notify.send_abandoned_cart_recovery(
            db, business=business, customer=customer, checkout_url=checkout.checkout_url,
        ):
            sent += 1

    return sent


async def send_repeat_purchase_nudges(db: AsyncSession) -> int:
    """
    order_sync capability. A delivered order with no later order for that
    same customer, 14-21 days on - see docs/d2c-research.md for why this
    targets retention (77% of first-time Indian D2C buyers never
    repurchase) rather than review collection, which send_review_request
    already covers.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Order).where(
            Order.status == OrderStatus.delivered,
            Order.repeat_purchase_nudge_sent_at.is_(None),
            Order.placed_at <= now - _REPEAT_PURCHASE_MIN_DAYS,
            Order.placed_at >= now - _REPEAT_PURCHASE_MAX_DAYS,
        )
    )
    candidates = list(result.scalars().all())
    if not candidates:
        return 0

    sent = 0
    for order in candidates:
        order.repeat_purchase_nudge_sent_at = now  # stamped regardless - see review-request precedent above
        if order.customer_id is None:
            continue
        business = await db.get(Business, order.business_id)
        if business is None or not verticals.has_capability(business.vertical, "order_sync"):
            continue

        later_order = (
            await db.execute(
                select(Order.id).where(
                    Order.customer_id == order.customer_id,
                    Order.business_id == order.business_id,
                    Order.placed_at > order.placed_at,
                ).limit(1)
            )
        ).scalars().first()
        if later_order is not None:
            continue  # already came back - a real repeat purchase, nothing to nudge

        customer = await db.get(Customer, order.customer_id)
        if customer is None:
            continue
        if await notify.send_repeat_purchase_nudge(db, business=business, customer=customer):
            sent += 1

    return sent
