"""
When a Claude prompt-cache write is worth paying for.

A cache write costs 1.25x the normal input price (5-minute TTL); every read
of it inside those 5 minutes costs 0.1x. So marking a prefix pays off only
when another request with the same prefix is likely to follow within 5
minutes - roughly, when more than one read per four writes lands. A
business that gets one message an hour would pay the 25% premium on every
reply and never read it back, which is worse than not caching at all.

Recent inbound traffic for the business is the signal: a second message in
the last ACTIVE_WINDOW means a conversation (or several) is live, and the
next reply or extraction for this business is likely minutes away. Cheap
and deterministic, and deliberately per business - a platform-wide count
would need a scan of messages without a business_id-leading index.

The live voice path does not use this: a call is many turns seconds apart,
so it always caches.
"""

from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Direction, Message

ACTIVE_WINDOW = timedelta(minutes=10)


async def business_recently_active(business_id: uuid.UUID, db: AsyncSession) -> bool:
    """True when this business had at least two inbound messages in ACTIVE_WINDOW."""
    since = datetime.now(timezone.utc) - ACTIVE_WINDOW
    result = await db.execute(
        select(Message.id)
        .where(
            Message.business_id == business_id,
            Message.direction == Direction.inbound,
            Message.occurred_at >= since,
        )
        .limit(2)
    )
    return len(result.scalars().all()) >= 2
