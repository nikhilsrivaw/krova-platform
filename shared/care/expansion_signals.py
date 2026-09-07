"""
The usage-based expansion nudge - product_feedback capability.

Research: top SaaS companies get 50%+ of new ARR from existing-customer
expansion, and most companies leave 40-60% of that revenue on the table
for lack of any system tracking when a customer is ready for it. Krova
only knows a usage milestone was hit because the business's own product
told it (CustomerLifecycleEvent, via POST /public-api/v1/lifecycle-events
- reusing the exact table and endpoint shared/care/onboarding_dropoff.py
already built for trial-activation tracking, not a new integration).

Unlike onboarding-dropoff, there is no window to wait out here - a usage
milestone is itself the moment worth acting on, not something to let sit
for a few days first, so this sweeps and sends promptly.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared import verticals
from shared.db.models import Business, Customer, CustomerLifecycleEvent
from shared.scheduling import notify
from shared.utils.logging import get_logger

logger = get_logger(__name__)


async def send_expansion_nudges(db: AsyncSession) -> int:
    """Send one nudge per not-yet-nudged usage_threshold_reached lifecycle
    event. Returns how many actually sent."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(CustomerLifecycleEvent).where(
            CustomerLifecycleEvent.event == "usage_threshold_reached",
            CustomerLifecycleEvent.nudge_sent_at.is_(None),
        )
    )
    due = list(result.scalars().all())
    if not due:
        return 0

    sent = 0
    for event in due:
        # Stamped regardless of outcome below - same "never reprocess"
        # discipline as every other sweep dedupe column in this codebase.
        event.nudge_sent_at = now

        business = await db.get(Business, event.business_id)
        if business is None or not verticals.has_capability(business.vertical, "product_feedback"):
            continue

        customer = await db.get(Customer, event.customer_id)
        if customer is None:
            continue

        if await notify.send_expansion_nudge(db, business=business, customer=customer):
            sent += 1

    return sent
