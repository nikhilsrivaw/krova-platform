"""
The onboarding-dropoff nudge - product_feedback capability.

Research: a user who doesn't reach a product's core value within 3 days
rarely converts to paid. Krova only knows a trial started because the
business told it (CustomerLifecycleEvent, via POST /public-api/v1/
lifecycle-events - see that model's own docstring for why this is a
real, disclosed dependency, not something Krova infers on its own).

Same time-window-scan shape as every other sweep in this codebase (see
shared/scheduling/recall.py's own reasoning) - a trial_started event that
sits unnudged past the window is picked up next run until it is, then
nudge_sent_at stops it matching again.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared import verticals
from shared.db.models import Business, Customer, CustomerLifecycleEvent
from shared.scheduling import notify
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# The research's own predictive window: a trial_started event this old,
# with no activated event since, is the point a nudge is worth sending -
# not so early it interrupts someone still exploring, not so late the
# trial has likely already been abandoned for good.
_MIN_AGE = timedelta(days=3)
_MAX_AGE = timedelta(days=5)


async def send_onboarding_dropoff_nudges(db: AsyncSession) -> int:
    """Send one nudge per trial_started customer who hasn't activated.
    Returns how many actually sent."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(CustomerLifecycleEvent).where(
            CustomerLifecycleEvent.event == "trial_started",
            CustomerLifecycleEvent.nudge_sent_at.is_(None),
            CustomerLifecycleEvent.occurred_at <= now - _MIN_AGE,
            CustomerLifecycleEvent.occurred_at >= now - _MAX_AGE,
        )
    )
    due = list(result.scalars().all())
    if not due:
        return 0

    sent = 0
    for trial_event in due:
        # Stamped regardless of outcome below - same "never reprocess"
        # discipline as every other sweep dedupe column in this codebase.
        trial_event.nudge_sent_at = now

        business = await db.get(Business, trial_event.business_id)
        if business is None or not verticals.has_capability(business.vertical, "product_feedback"):
            continue

        activated = (
            await db.execute(
                select(CustomerLifecycleEvent.id).where(
                    CustomerLifecycleEvent.customer_id == trial_event.customer_id,
                    CustomerLifecycleEvent.event == "activated",
                ).limit(1)
            )
        ).scalars().first()
        if activated is not None:
            continue  # already activated - nothing to nudge

        customer = await db.get(Customer, trial_event.customer_id)
        if customer is None:
            continue

        if await notify.send_onboarding_nudge(db, business=business, customer=customer):
            sent += 1

    return sent
