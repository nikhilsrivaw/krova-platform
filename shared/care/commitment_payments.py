"""
Money actually received against a promise - including part of it.

"₹15,000 fees, ₹8,000 aa gaye, baaki next week" is how instalments, deposits
and retainers really get paid. Before this module a commitment could only be
open, met or missed, so an owner had two wrong choices: leave ₹15,000
showing as owed when half has arrived, or mark it met and lose the ₹7,000
still outstanding. Both make the Ledger lie.

So a commitment keeps what was promised (`amount_paise`, never edited here)
and separately accumulates what arrived (`amount_received_paise`), with
every receipt kept in `payment_log` so "who recorded ₹8,000, and when" is
always answerable. What is still owed is the difference -
`Commitment.outstanding_paise`, and `OUTSTANDING` below for SQL.

Every path that records money goes through record_payment: a person on the
Ledger and a Meta-verified WhatsApp payment
(services/api/routers/webhooks.py) alike. One rule for when a promise is
kept, in one place.

What this deliberately does not do: treat a customer *saying* they paid
("8000 bhej diya") as a receipt. That is a claim, not money - the same
"never invent a fact" line commitments.py draws. A person confirms it, or a
verified payment does.
"""

from datetime import datetime, timezone
import uuid

from sqlalchemy import func

from shared.db.models import Commitment, CommitmentStatus

# What is still owed on an open commitment, as a SQL expression. NULL when
# no amount was ever stated - summed as nothing, exactly as an amount-less
# promise always has been. Floored at zero so an overpayment never shows up
# as negative money owed.
OUTSTANDING = func.greatest(Commitment.amount_paise - Commitment.amount_received_paise, 0)


class PaymentError(ValueError):
    """Raised for a receipt that can't be recorded - shown to the user as-is."""


def record_payment(
    commitment: Commitment,
    *,
    amount_paise: int,
    source: str,
    recorded_by_user_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> bool:
    """
    Add a receipt to an open commitment. Returns True if this receipt
    closed it (the full promised amount has now arrived).

    `source` is where the receipt came from - "manual" for a person on the
    Ledger, "whatsapp_pay" for a verified WhatsApp payment - and is kept on
    the receipt, never interpreted.

    A commitment with no stated amount can still take a receipt, but can
    never close itself this way: there is no total to reach. A person
    closes it with the ordinary resolve action once they consider it paid.
    """
    if amount_paise <= 0:
        raise PaymentError("A payment must be more than zero")
    if commitment.status != CommitmentStatus.open:
        status = commitment.status.value if hasattr(commitment.status, "value") else commitment.status
        raise PaymentError(f"This commitment is {status}, not open")

    now = now or datetime.now(timezone.utc)
    commitment.amount_received_paise = (commitment.amount_received_paise or 0) + amount_paise
    # Reassigned rather than appended in place, so SQLAlchemy sees the
    # JSONB column change without needing a mutable-tracking wrapper.
    commitment.payment_log = [
        *(commitment.payment_log or []),
        {
            "amount_paise": amount_paise,
            "at": now.isoformat(),
            "source": source,
            "recorded_by_user_id": str(recorded_by_user_id) if recorded_by_user_id else None,
        },
    ]

    if commitment.amount_paise is not None and commitment.amount_received_paise >= commitment.amount_paise:
        commitment.status = CommitmentStatus.met
        commitment.resolved_at = now
        return True
    return False
