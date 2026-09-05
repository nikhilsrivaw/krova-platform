"""
The clinic daily-briefing sweep - care_recall capability.

Turns an overdue Commitment into something the owner actually sees. Same
dedupe discipline as services/workers/analyse.py's signal extraction
(_existing_signal_titles): title-fingerprint matching, approximate rather
than a hard foreign key back to the Commitment - the same tradeoff
analyse.py's own docstring already accepts ("approximate, ... good enough
for the same reason"), applied here to a plain deterministic scan instead
of an LLM extraction.

Deliberately scans only kind in (meeting, document) - not "other", which is
where referral commitments live. Referral-loop-closure is parked; this sweep
must not silently start surfacing it as a side effect of scanning too
broadly.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared import verticals
from shared.db.models import Business, Commitment, CommitmentKind, CommitmentStatus, Insight
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# A commitment isn't "overdue" the instant its due_at passes - a same-day
# grace avoids flagging something merely a few hours late.
_GRACE = timedelta(days=1)

_KIND_TO_INSIGHT = {
    CommitmentKind.meeting: "overdue_followup",
    CommitmentKind.document: "report_not_collected",
}


async def _existing_titles(business_id, db: AsyncSession) -> set[str]:
    result = await db.execute(select(Insight.title).where(Insight.business_id == business_id))
    return {(t or "").strip().lower() for t in result.scalars().all()}


def _title_for(commitment: Commitment) -> str:
    prefix = "Overdue follow-up" if commitment.kind == CommitmentKind.meeting else "Not yet collected"
    return f"{prefix}: {commitment.description}".strip()


async def check_clinic_commitments(db: AsyncSession) -> int:
    """Scan overdue commitments for clinic businesses and emit Insight rows. Returns how many created."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Business).where(
            Business.is_active.is_(True),
            Business.vertical == "clinic",
        )
    )
    businesses = [b for b in result.scalars().all() if verticals.has_capability(b.vertical, "care_recall")]
    if not businesses:
        return 0

    created = 0
    for business in businesses:
        existing = await _existing_titles(business.id, db)
        rows = await db.execute(
            select(Commitment).where(
                Commitment.business_id == business.id,
                Commitment.kind.in_(list(_KIND_TO_INSIGHT)),
                Commitment.status == CommitmentStatus.open,
                Commitment.due_at.is_not(None),
                Commitment.due_at <= now - _GRACE,
            )
        )
        for commitment in rows.scalars().all():
            title = _title_for(commitment)
            if title.strip().lower() in existing:
                continue
            db.add(
                Insight(
                    business_id=business.id,
                    customer_id=commitment.customer_id,
                    kind=_KIND_TO_INSIGHT[commitment.kind],
                    title=title,
                    body=commitment.description,
                    severity="warning",
                    source_message_ids=commitment.source_message_ids,
                    created_at=now,
                )
            )
            existing.add(title.strip().lower())
            created += 1

    if created:
        logger.info("created %s clinic overdue-commitment insight(s)", created)
    return created
