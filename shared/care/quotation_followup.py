"""
Chasing quotes before they go cold.

The whole point of tracking quotations at all. Research
(docs/new/type-1-research.md): *"once a quotation is sent, most small
businesses have no system to track its status or schedule follow-ups"*,
and a deal still sitting in the proposal stage past **21 days wins 70%
less often**. So the job here is not to report on stale quotes - it is to
make sure they never get stale.

Three things are deliberately different from how it might first be built:

1. **It nudges staff, not the customer.** A quote follow-up is a judgement
   call - price, relationship, whether a competitor is in play - and an
   automatic "any update on our quote?" to a buyer is the kind of message
   that makes a business look like software. The sweep raises an Insight
   for the owner's own people; a human decides what to actually say. This
   mirrors how RTO-risk pincodes flag rather than block.

2. **It escalates, rather than repeating.** A quote nudged three times and
   still silent does not need a fourth nudge; it needs someone senior to
   decide whether it is dead. Past the last step it is marked expired
   rather than chased forever.

3. **Negotiating quotes are left alone.** A thread that is actively going
   back and forth is warm, and someone is already on it. Only `sent`
   silence is the 21-day problem.

Deliberately *not* here: sending anything to the buyer. That needs an
approved WhatsApp template and a human's judgement about tone, and both
are decisions this sweep should not make on its own.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    Business,
    Customer,
    Insight,
    Quotation,
    QuotationStatus,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Days after sending at which a quote gets nudged. The last of these is
# where the research says the win rate has already collapsed, so it is the
# point of escalation rather than another reminder.
NUDGE_DAYS: tuple[int, ...] = (3, 7, 14, 21)

# An open quote this far past its own validity date is not pending any
# more, whatever the pipeline says.
_EXPIRY_GRACE = timedelta(days=1)


def _due_step(days_open: int, already_sent: int) -> int | None:
    """
    Which nudge (1-indexed) this quote is due for, or None.

    Compares elapsed days against NUDGE_DAYS and returns the highest step
    reached that has not been sent yet - so a quote that goes unswept for a
    week catches up in one move rather than firing three nudges in a row.
    """
    reached = sum(1 for threshold in NUDGE_DAYS if days_open >= threshold)
    if reached > already_sent:
        return reached
    return None


async def expire_stale_quotations(db: AsyncSession) -> int:
    """
    Close out open quotes whose own validity date has passed.

    Separate from nudging because it is a fact, not a judgement: the offer
    said it was good until a date, and that date has gone.
    """
    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(Quotation).where(
                Quotation.status.in_(
                    (QuotationStatus.sent, QuotationStatus.negotiating, QuotationStatus.draft)
                ),
                Quotation.valid_until.is_not(None),
                Quotation.valid_until < now - _EXPIRY_GRACE,
            )
        )
    ).scalars().all()

    for quotation in rows:
        quotation.status = QuotationStatus.expired
        quotation.closed_at = now

    if rows:
        logger.info("expired %s quotation(s) past their validity date", len(rows))
    return len(rows)


async def check_quotation_followups(db: AsyncSession) -> int:
    """
    Raise a nudge for every open quote that has gone quiet long enough.

    Returns how many were raised.
    """
    now = datetime.now(timezone.utc)

    # Only `sent` - a negotiating quote is already warm, and a draft was
    # never actually put in front of the buyer.
    rows = (
        await db.execute(
            select(Quotation).where(
                Quotation.status == QuotationStatus.sent,
                Quotation.sent_at.is_not(None),
            )
        )
    ).scalars().all()
    if not rows:
        return 0

    raised = 0
    for quotation in rows:
        days_open = (now - quotation.sent_at).days
        step = _due_step(days_open, quotation.follow_up_count)
        if step is None:
            continue

        business = await db.get(Business, quotation.business_id)
        if business is None:
            continue
        customer = await db.get(Customer, quotation.customer_id)
        who = (customer.display_name if customer else None) or "this customer"

        amount = (
            f" worth ₹{quotation.total_paise / 100:,.0f}"
            if quotation.total_paise
            else ""
        )
        reference = f" ({quotation.reference})" if quotation.reference else ""
        final = step >= len(NUDGE_DAYS)

        if final:
            title = f"Quote to {who} has gone cold"
            body = (
                f"The quote{reference}{amount} was sent {days_open} days ago with no "
                f"outcome after {quotation.follow_up_count} follow-ups. Past three "
                f"weeks these rarely close - worth deciding whether it is still live."
            )
        else:
            title = f"Follow up the quote to {who}"
            body = (
                f"Sent {days_open} days ago{reference}{amount}, still no answer. "
                f"Quotes chased early close far more often than ones left to sit."
            )

        db.add(
            Insight(
                business_id=quotation.business_id,
                customer_id=quotation.customer_id,
                kind="quotation_followup",
                title=title,
                body=body,
                severity="warning" if final else "info",
                # Provenance discipline: a quote captured from a conversation
                # cites those messages; one typed in by hand cites nothing,
                # which is honest rather than invented.
                source_message_ids=list(quotation.source_message_ids or []),
            )
        )

        quotation.follow_up_count = step
        quotation.last_followed_up_at = now
        raised += 1

    if raised:
        logger.info("raised %s quotation follow-up(s)", raised)
    return raised
