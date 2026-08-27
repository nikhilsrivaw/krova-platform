"""
Tags that suggest themselves.

Called once per customer, right after the nightly profile compression writes
a fresh CustomerIntelligence row - so every rule here reads data that was
just computed, never stale. Each rule is a deterministic read of something
already in the ledger (a health score, a missed commitment, a signal already
extracted from a real conversation) - never a new AI call. That is a
deliberate choice: a second LLM guessing at a customer's character on top of
the one that already wrote the health score would be two opinions competing
for the same slot, and neither would be more trustworthy than the first.

Every proposal is written as TagStatus.suggested with its reasoning attached
and nothing more happens until a human confirms or rejects it in the CRM -
see the module docstring on shared.db.models.crm.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    Commitment,
    CommitmentDirection,
    CommitmentStatus,
    CustomerIntelligence,
    CustomerTag,
    Insight,
    TagStatus,
)

# Named here rather than buried in the conditionals below, so tuning them
# later is a one-line change, not a hunt through the rule bodies.
HIGH_VALUE_OUTSTANDING_PAISE = 2_000_000  # ₹20,000
AT_RISK_HEALTH_SCORE = 35
RELIABLE_HEALTH_SCORE = 80
CHRONIC_MISSED_COMMITMENTS = 2
REPEAT_COMPLAINT_COUNT = 2
SIGNAL_LOOKBACK_DAYS = 90


@dataclass(slots=True)
class SuggestedTag:
    label: str
    reasoning: str


async def suggest(
    customer_id: uuid.UUID,
    intelligence: CustomerIntelligence,
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> list[SuggestedTag]:
    """Work out which tags this customer's real history currently supports."""
    now = now or datetime.now(timezone.utc)
    proposals: list[SuggestedTag] = []

    if intelligence.health_score >= RELIABLE_HEALTH_SCORE and intelligence.open_commitments == 0:
        proposals.append(SuggestedTag(
            "reliable",
            f"Health score {intelligence.health_score}/100 with nothing currently outstanding.",
        ))
    elif intelligence.health_score < AT_RISK_HEALTH_SCORE:
        proposals.append(SuggestedTag(
            "at-risk", f"Health score {intelligence.health_score}/100.",
        ))

    if intelligence.outstanding_paise >= HIGH_VALUE_OUTSTANDING_PAISE:
        proposals.append(SuggestedTag(
            "high-value",
            f"₹{intelligence.outstanding_paise / 100:,.0f} currently outstanding.",
        ))

    missed = (
        await db.execute(
            select(Commitment.id).where(
                Commitment.customer_id == customer_id,
                Commitment.direction == CommitmentDirection.they_owe,
                Commitment.status == CommitmentStatus.missed,
            )
        )
    ).all()
    if len(missed) >= CHRONIC_MISSED_COMMITMENTS:
        proposals.append(SuggestedTag(
            "chases payment", f"{len(missed)} missed payment commitments on record.",
        ))

    since = now - timedelta(days=SIGNAL_LOOKBACK_DAYS)
    signal_rows = (
        await db.execute(
            select(Insight.kind).where(
                Insight.customer_id == customer_id, Insight.created_at >= since,
            )
        )
    ).all()
    kinds = [k for (k,) in signal_rows]

    if kinds.count("churn_risk") >= 1:
        proposals.append(SuggestedTag(
            "flight-risk",
            f"A churn-risk signal was read from a conversation in the last {SIGNAL_LOOKBACK_DAYS} days.",
        ))
    complaint_count = kinds.count("complaint")
    if complaint_count >= REPEAT_COMPLAINT_COUNT:
        proposals.append(SuggestedTag(
            "repeat complainer",
            f"{complaint_count} complaints read from conversations in the last {SIGNAL_LOOKBACK_DAYS} days.",
        ))
    if kinds.count("praise") >= 1 and intelligence.health_score >= RELIABLE_HEALTH_SCORE:
        proposals.append(SuggestedTag(
            "advocate",
            f"Praise on record and a health score of {intelligence.health_score}/100.",
        ))

    return proposals


async def apply_suggestions(
    customer_id: uuid.UUID,
    business_id: uuid.UUID,
    intelligence: CustomerIntelligence,
    db: AsyncSession,
) -> int:
    """
    Write any newly-supported tags as suggestions.

    Skips any label already decided one way or the other, or already
    pending - a rejected tag must never reappear just because the rule that
    proposed it fired again on the next nightly pass. Returns how many new
    suggestions were written.
    """
    proposals = await suggest(customer_id, intelligence, db)
    if not proposals:
        return 0

    existing_labels = {
        label
        for (label,) in (
            await db.execute(
                select(CustomerTag.label).where(CustomerTag.customer_id == customer_id)
            )
        ).all()
    }

    created = 0
    for p in proposals:
        if p.label in existing_labels:
            continue
        db.add(CustomerTag(
            business_id=business_id,
            customer_id=customer_id,
            label=p.label,
            status=TagStatus.suggested,
            reasoning=p.reasoning,
        ))
        created += 1
    return created
