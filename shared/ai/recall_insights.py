"""
Daily-briefing overdue-commitment sweep - one generic mechanism,
configured per vertical by that vertical's own watch_for block
(shared/verticals/templates/*.json), not a hardcoded function per
vertical. Every vertical currently declares watch_for entries
(confirmed: clinic, ecommerce, law_firm, real_estate, restaurant,
general, startup all do) - this sweep covers every one of them the
moment their template names a kind, with zero new Python needed to
add the next vertical.

Replaces the former check_clinic_commitments/check_ecommerce_commitments
split, which gated on Business.vertical == "clinic"/"ecommerce" string
checks - the exact per-vertical-fork pattern shared/verticals/__init__.py's
own docstring warns against. shared/ai/commitments.py's own extraction was
already vertical-agnostic; only the surfacing sweep was not.

Four CommitmentKind values are handled, matching what recall_insights has
ever surfaced - "other" (referral-loop-closure, explicitly parked) and
"bug_fix" (its own separate closed-loop, not this sweep) stay out of
scope, unchanged from before:

  meeting    -> overdue_followup     a promised meeting/follow-up didn't happen
  document   -> report_not_collected a promised document hasn't arrived either way
  callback   -> callback_overdue     every vertical's watch_for names this kind, but
                                      neither prior sweep ever mapped it - a real gap
  payment/delivery, direction=we_owe -> overdue_refund   the business owes the
                                      customer money or goods and it's late; a
                                      they_owe payment/delivery is already visible
                                      directly on the Ledger's own summary, not a
                                      signal, so it's deliberately left alone here

Same dedupe discipline as every other sweep in this codebase: title-
fingerprint matching via _existing_titles, approximate rather than a hard
foreign key back to the Commitment (see services/workers/analyse.py's own
docstring for why this tradeoff is accepted throughout).
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared import verticals
from shared.care.signal_dispatch import dispatch_signal
from shared.db.models import (
    Business,
    Commitment,
    CommitmentDirection,
    CommitmentKind,
    CommitmentStatus,
    Insight,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# A commitment isn't "overdue" the instant its due_at passes - a same-day
# grace avoids flagging something merely a few hours late.
_GRACE = timedelta(days=1)

_KIND_TO_INSIGHT = {
    CommitmentKind.meeting: ("overdue_followup", "Overdue follow-up"),
    CommitmentKind.document: ("report_not_collected", "Not yet collected"),
    CommitmentKind.callback: ("callback_overdue", "Callback overdue"),
}

_WE_OWE_TITLE = {
    CommitmentKind.payment: "Refund owed",
    CommitmentKind.delivery: "Replacement owed",
}


async def _existing_titles(business_id, db: AsyncSession) -> set[str]:
    result = await db.execute(select(Insight.title).where(Insight.business_id == business_id))
    return {(t or "").strip().lower() for t in result.scalars().all()}


def _resolve(commitment: Commitment) -> tuple[str, str] | None:
    """(insight_kind, title) for a commitment worth surfacing, or None if
    this kind/direction combination isn't one recall_insights covers."""
    if commitment.kind in _KIND_TO_INSIGHT:
        insight_kind, prefix = _KIND_TO_INSIGHT[commitment.kind]
        return insight_kind, f"{prefix}: {commitment.description}".strip()
    if commitment.kind in _WE_OWE_TITLE and commitment.direction == CommitmentDirection.we_owe:
        prefix = _WE_OWE_TITLE[commitment.kind]
        return "overdue_refund", f"{prefix}: {commitment.description}".strip()
    return None


async def check_overdue_commitments(db: AsyncSession) -> int:
    """
    Scan every active business's overdue commitments for the kinds its own
    vertical template names in watch_for, and surface each as an Insight.
    Returns how many were created.
    """
    now = datetime.now(timezone.utc)
    businesses = (
        await db.execute(select(Business).where(Business.is_active.is_(True)))
    ).scalars().all()

    created = 0
    for business in businesses:
        try:
            template = verticals.get(business.vertical)
        except verticals.UnknownVertical:
            # A business row can carry a stale vertical string (e.g. one
            # since removed from the template set) - previously invisible
            # to this sweep since it queried per-vertical at the SQL level;
            # now that every business is scanned, skip it rather than let
            # one bad row fail the sweep for every other business.
            logger.warning("skipping unknown vertical %r for business=%s", business.vertical, business.id)
            continue
        watched = {w["kind"] for w in template.get("watch_for", [])}
        relevant_kinds = [k for k in CommitmentKind if k.value in watched]
        if not relevant_kinds:
            continue

        existing = await _existing_titles(business.id, db)
        rows = await db.execute(
            select(Commitment).where(
                Commitment.business_id == business.id,
                Commitment.kind.in_(relevant_kinds),
                Commitment.status == CommitmentStatus.open,
                Commitment.due_at.is_not(None),
                Commitment.due_at <= now - _GRACE,
            )
        )
        for commitment in rows.scalars().all():
            resolved = _resolve(commitment)
            if resolved is None:
                continue
            insight_kind, title = resolved
            if title.strip().lower() in existing:
                continue
            db.add(
                Insight(
                    business_id=business.id,
                    customer_id=commitment.customer_id,
                    kind=insight_kind,
                    title=title,
                    body=commitment.description,
                    severity="warning",
                    source_message_ids=commitment.source_message_ids,
                    created_at=now,
                )
            )
            await dispatch_signal(
                db, business_id=business.id, customer_id=commitment.customer_id, channel=None,
                kind=insight_kind, title=title, body=commitment.description, severity="warning",
            )
            existing.add(title.strip().lower())
            created += 1

    if created:
        logger.info("created %s overdue-commitment insight(s)", created)
    return created
