"""
The proactive deadline-call sweep - opt-in, cross-vertical.

A commitment (any kind: payment, delivery, callback, document, ...)
whose due_at is coming up within the lead window gets a real voice call,
not a text nudge - research on proactive outreach consistently finds a
call converts better than a message for something time-sensitive, and
this is the one channel KROVA can place unprompted. Explicitly opt-in per
business (Business.settings["proactive_deadline_calls_enabled"]) - a
phone call is a meaningfully bigger interruption than a WhatsApp/email
nudge, and must never fire just because a business happens to have a
voice number connected. Same time-window-scan shape as every other sweep
in this codebase (see shared/scheduling/recall.py's own reasoning).

Deliberately not built on reminder_sent_at - that column is already
spoken for by the clinic-only chronic-care WhatsApp recall
(shared/scheduling/recall.py::send_due_recalls, kind=meeting only); see
Commitment.deadline_call_sent_at's own docstring for why sharing it would
have let one send silently block the other.

Out of scope, and not silently assumed here: "stock arrived" or any other
event-triggered call - no inventory-watch integration exists anywhere in
this codebase to build that on. This sweep is deadline-driven only.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    Business,
    Channel,
    ChannelConnection,
    Commitment,
    CommitmentStatus,
    ConnectionStatus,
    Customer,
)
from shared.scheduling import notify
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# How far ahead of due_at a call is worth placing - early enough to be
# useful advance notice, not so early it reads as premature.
_LEAD_TIME = timedelta(hours=24)


async def check_deadline_calls(db: AsyncSession) -> int:
    """Place one proactive call per commitment approaching its deadline.
    Returns how many calls were actually placed."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Commitment).where(
            Commitment.status == CommitmentStatus.open,
            Commitment.deadline_call_sent_at.is_(None),
            Commitment.due_at.is_not(None),
            Commitment.due_at >= now,
            Commitment.due_at <= now + _LEAD_TIME,
        )
    )
    due = list(result.scalars().all())
    if not due:
        return 0

    placed = 0
    for commitment in due:
        # Stamped regardless of outcome below - same "never reprocess"
        # discipline as every other sweep dedupe column in this codebase
        # (see onboarding_dropoff.py's own comment on this exact line).
        commitment.deadline_call_sent_at = now

        business = await db.get(Business, commitment.business_id)
        if business is None:
            continue
        if not (business.settings or {}).get("proactive_deadline_calls_enabled"):
            continue

        connection = (
            await db.execute(
                select(ChannelConnection.id).where(
                    ChannelConnection.business_id == business.id,
                    ChannelConnection.channel == Channel.voice,
                    ChannelConnection.status == ConnectionStatus.active,
                ).limit(1)
            )
        ).scalars().first()
        if connection is None:
            continue

        customer = await db.get(Customer, commitment.customer_id)
        if customer is None:
            continue

        reason = f"Remind the customer about: {commitment.description}"
        if await notify.send_deadline_call(db, business=business, customer=customer, reason=reason):
            placed += 1

    return placed
