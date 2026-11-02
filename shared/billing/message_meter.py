"""
The plan's monthly customer messages (shared/billing/limits.py `messages`): told to the owner on every plan, and on
Solo enforced.

  - Every plan: the owner (and admins) get a push at 80% and at 100%, once each a month.
  - Solo only: after the size plus a 10% grace (500 + 50), the AI stops replying on WhatsApp and Instagram until the
    1st of next month or a move to a bigger plan. Messages still arrive in the inbox and the team replies there;
    an emergency, a request for a person and the feedback/QR fixed replies still work (services/workers/respond.py
    checks this after them). Calls are paid from the wallet and never counted.

The count is limits.messages_this_month (since the 1st, UTC), the same number the Billing page shows.
"""

import math
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.billing import limits
from shared.utils.logging import get_logger

logger = get_logger(__name__)

GRACE_PCT = 10
# Plans where the AI stops after the grace. The others are told, never stopped.
ENFORCED = frozenset({"solo"})
NOTICE_AT = (80, 100)


def stop_at(plan: str | None) -> int | None:
    """The message count at which the AI stops replying on this plan; None = never."""
    cap = limits.limits_for(plan)
    if cap is None or (plan or "").lower() not in ENFORCED:
        return None
    return math.ceil(cap.messages * (100 + GRACE_PCT) / 100)


async def ai_replies_stopped(db: AsyncSession, business) -> bool:
    """True when this business's plan has used its messages and grace this month."""
    limit = stop_at(getattr(business, "plan", None))
    if limit is None:
        return False
    return await limits.messages_this_month(db, business.id) >= limit


def _month(now: datetime) -> str:
    return now.strftime("%Y-%m")


async def send_notices(db: AsyncSession, now: datetime | None = None) -> int:
    """Push 80% / 100% (and, on Solo, "replies stopped") to owners, once each a month. Returns how many went."""
    from shared.billing.wallet import _tell_owner
    from shared.db.models import Business

    now = now or datetime.now(timezone.utc)
    month = _month(now)
    sent = 0
    rows = (await db.execute(
        select(Business).where(Business.plan.in_(tuple(limits.PLAN_LIMITS)), Business.is_active.is_(True))
    )).scalars().all()
    for business in rows:
        cap = limits.limits_for(business.plan)
        used = await limits.messages_this_month(db, business.id)
        settings = dict(business.settings or {})
        done = settings.get("message_notices") or {}
        told = list(done.get("sent") or []) if done.get("month") == month else []
        plan = limits.PLAN_NAMES[business.plan.lower()]
        due = [p for p in NOTICE_AT if used >= cap.messages * p / 100 and p not in told]
        stop = stop_at(business.plan)
        if stop is not None and used >= stop and "stopped" not in told:
            due.append("stopped")
        if not due:
            continue
        last = due[-1]
        if last == "stopped":
            title, body = ("Automatic replies paused",
                           f"Your {plan} plan's {cap.messages} customer messages and the extra {stop - cap.messages} are used "
                           "this month. Messages still come into the inbox - reply there, or move to Starter on the Billing page.")
        elif last == 100:
            title, body = (f"{cap.messages} customer messages used",
                           f"Your {plan} plan is built for {cap.messages} a month."
                           + (f" Automatic replies pause at {stop}." if stop else " Moving up is on the Billing page."))
        else:
            title, body = (f"{used} of {cap.messages} customer messages used",
                           f"You have used 80% of your {plan} plan's messages this month.")
        await _tell_owner(db, business.id, title, body)
        settings["message_notices"] = {"month": month, "sent": told + due}
        business.settings = settings
        sent += 1
        logger.info("message notice business=%s plan=%s used=%s notice=%s", business.id, business.plan, used, last)
    return sent


async def usage_for(db: AsyncSession, business_id: uuid.UUID, plan: str | None) -> dict:
    """For the Billing page: where the AI stops (if it does) and whether it has."""
    limit = stop_at(plan)
    used = await limits.messages_this_month(db, business_id)
    return {"stop_at": limit, "ai_stopped": limit is not None and used >= limit}
