"""
The voice trust signal - a business's own real call-answering numbers,
readable both privately (the business itself) and, opt-in, publicly.

Distinct from services/api/routers/analytics.py's existing Trust Report,
which is text-channel only (computed from MessageDraft's confidence/
citations). This one is voice-only, computed from Call rows, and the one
genuinely new figure it can report - time to answer - only became
meaningful once shared/channels/voice/call_registry.py started capturing
a real ring_started_at (see Call.ring_started_at's own docstring): before
that, started_at/answered_at were both stamped at the instant the AI's
media stream connected, so "time to answer" would have trivially read as
a constant near-zero for every call - not a real number, and not honest
to publish as one.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Call


@dataclass(slots=True)
class VoiceTrustStats:
    window_days: int
    total_calls: int
    # None (not 0) when no call in the window has a ring_started_at yet -
    # distinct from "answered instantly every time", the same never-invent
    # convention every other None-means-"not tracked" field in this
    # codebase already follows (see shared/ai/context.py's AgentContext).
    avg_ring_to_answer_seconds: float | None
    escalation_rate: float | None  # escalated calls / total_calls, None if total_calls == 0
    avg_duration_seconds: float | None


async def stats(business_id: uuid.UUID, db: AsyncSession, *, days: int = 30) -> VoiceTrustStats:
    """Real numbers only - every field here reads directly off Call rows, nothing estimated."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    base = (Call.business_id == business_id, Call.started_at >= since)

    total = (await db.execute(select(func.count(Call.id)).where(*base))).scalar_one()

    ring_avg = (
        await db.execute(
            select(
                func.avg(func.extract("epoch", Call.started_at - Call.ring_started_at))
            ).where(*base, Call.ring_started_at.is_not(None))
        )
    ).scalar_one()

    escalated_count = (
        await db.execute(
            select(func.count(Call.id)).where(*base, Call.escalated.is_(True))
        )
    ).scalar_one()

    duration_avg = (
        await db.execute(
            select(func.avg(Call.duration_seconds)).where(*base, Call.duration_seconds.is_not(None))
        )
    ).scalar_one()

    return VoiceTrustStats(
        window_days=days,
        total_calls=int(total),
        avg_ring_to_answer_seconds=float(ring_avg) if ring_avg is not None else None,
        escalation_rate=(escalated_count / total) if total else None,
        avg_duration_seconds=float(duration_avg) if duration_avg is not None else None,
    )
