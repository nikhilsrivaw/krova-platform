"""
Writing to the usage ledger.

One function, called from every place that already knows a cost - the agent
reply paths, the extraction/compression workers, voice's call finaliser,
ingest() itself for message volume. None of those need to know anything
about billing plans or pricing; they just report what happened. Turning
that into a bill is a separate, later concern (a monthly rollup against
whatever plan a business is on) that reads this table, never writes it.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func as sql_func

from shared.db.models import UsageEvent, UsageEventType


def record(
    *,
    business_id: uuid.UUID,
    event_type: UsageEventType,
    channel: str,
    quantity: float,
    unit: str,
    krova_cost_paise: int = 0,
    source_type: str | None = None,
    source_id: uuid.UUID | None = None,
    extra: dict | None = None,
    occurred_at: datetime | None = None,
    db: AsyncSession,
) -> None:
    """
    Stage one usage row on the caller's own session - not flushed or
    committed here. `db.add()` is a purely in-memory operation that cannot
    itself fail, so this deliberately does no I/O and needs no error
    handling: the row rides on the SAME transaction as whatever the caller
    is already writing (a Message, a Call, a Commitment), and commits or
    rolls back atomically with it. A usage row for a reply that was never
    actually saved would be a charge for something that didn't happen;
    tying its lifetime to the real write is what prevents that.

    Synchronous on purpose - nothing here awaits, so callers that are
    themselves sync-looking one-liners don't need to change shape to call it.
    """
    if quantity <= 0:
        return
    db.add(
        UsageEvent(
            business_id=business_id,
            event_type=event_type,
            channel=channel,
            unit=unit,
            quantity=quantity,
            krova_cost_paise=krova_cost_paise,
            source_type=source_type,
            source_id=source_id,
            extra=extra or {},
            occurred_at=occurred_at or datetime.now(timezone.utc),
        )
    )


async def summarize(
    business_id: uuid.UUID, *, since: datetime, until: datetime, db: AsyncSession
) -> list[dict]:
    """
    One row per (event_type, channel) with total quantity and total Krova
    cost in the window - the shape a monthly rollup or a usage dashboard
    reads, not individual events.
    """
    result = await db.execute(
        select(
            UsageEvent.event_type,
            UsageEvent.channel,
            UsageEvent.unit,
            sql_func.sum(UsageEvent.quantity).label("total_quantity"),
            sql_func.sum(UsageEvent.krova_cost_paise).label("total_krova_cost_paise"),
            sql_func.count().label("event_count"),
        )
        .where(
            UsageEvent.business_id == business_id,
            UsageEvent.occurred_at >= since,
            UsageEvent.occurred_at < until,
        )
        .group_by(UsageEvent.event_type, UsageEvent.channel, UsageEvent.unit)
    )
    return [
        {
            "event_type": row.event_type.value,
            "channel": row.channel,
            "unit": row.unit,
            "total_quantity": float(row.total_quantity),
            "total_krova_cost_paise": int(row.total_krova_cost_paise),
            "event_count": row.event_count,
        }
        for row in result.all()
    ]
