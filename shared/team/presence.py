"""
"Rahul is replying" - who has a conversation open right now.

The open thread tells us every few seconds that it is open (and whether the
person is typing). A row older than WINDOW_SECONDS means they left. Presence is
advisory: it lets a teammate see the other person before sending a second answer,
and it never blocks anything - the hard guard is shared/team/conflict.py.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import ThreadPresence, User

WINDOW_SECONDS = 20


async def touch(
    db: AsyncSession, *, business_id: uuid.UUID, customer_id: uuid.UUID, user_id: uuid.UUID,
    typing: bool,
) -> None:
    now = datetime.now(timezone.utc)
    stmt = pg_insert(ThreadPresence).values(
        business_id=business_id, customer_id=customer_id, user_id=user_id,
        typing=typing, seen_at=now,
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=["business_id", "customer_id", "user_id"],
            set_={"typing": typing, "seen_at": now},
        )
    )
    # Opportunistic cleanup keeps the table to the handful of rows that matter.
    await db.execute(
        delete(ThreadPresence).where(
            ThreadPresence.business_id == business_id,
            ThreadPresence.seen_at < now - timedelta(minutes=10),
        )
    )


async def leave(
    db: AsyncSession, *, business_id: uuid.UUID, customer_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    await db.execute(
        delete(ThreadPresence).where(
            ThreadPresence.business_id == business_id,
            ThreadPresence.customer_id == customer_id,
            ThreadPresence.user_id == user_id,
        )
    )


async def viewers(
    db: AsyncSession, *, business_id: uuid.UUID, customer_id: uuid.UUID, exclude_user_id: uuid.UUID
) -> list[dict]:
    """Teammates (not the caller) who have this thread open within the window."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=WINDOW_SECONDS)
    rows = await db.execute(
        select(ThreadPresence, User)
        .join(User, User.id == ThreadPresence.user_id)
        .where(
            ThreadPresence.business_id == business_id,
            ThreadPresence.customer_id == customer_id,
            ThreadPresence.user_id != exclude_user_id,
            ThreadPresence.seen_at >= cutoff,
        )
        .order_by(ThreadPresence.seen_at.desc())
    )
    return [
        {
            "user_id": str(user.id),
            "name": user.full_name or user.username or user.email or "A teammate",
            "typing": bool(p.typing),
        }
        for p, user in rows.all()
    ]
