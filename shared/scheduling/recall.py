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
    Business,
    Commitment,
    CommitmentKind,
    CommitmentStatus,
    Customer,
)
from shared import verticals
from shared.scheduling import notify
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Same reasoning as reminders.py's _WINDOW: wide enough that a 30-minute poll
# cycle can never fully skip a due commitment even if one run is late.
_WINDOW = timedelta(hours=1)


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
