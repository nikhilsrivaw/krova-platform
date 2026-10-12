"""
Where an unpaid plan or an empty wallet actually stops things.

  require_active_plan   402 when the business's plan is suspended (AI sending,
                        campaigns, flows). Businesses with no subscription row -
                        everyone from before billing - are never stopped.
  require_wallet        402 for starting outbound calls when a wallet exists and is empty.

Reading data is never blocked: a suspended business can still see its customers
and pay.
"""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from shared.billing import wallet
from shared.billing.service import is_blocked
from shared.config.settings import settings
from shared.db.models import Business, Subscription


def must_have_plan(business) -> bool:
    """A business created on/after BILLING_ENFORCED_FROM has no free period: no plan, no AI sending."""
    cutoff = settings.billing_enforced_from.strip()
    if not cutoff or business is None or business.created_at is None:
        return False
    try:
        start = datetime.fromisoformat(cutoff)
    except ValueError:
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return business.created_at >= start


async def plan_state(db: AsyncSession, business_id: uuid.UUID) -> str | None:
    """
    Why a business is read-only, or None if it is not:
      required   it has never paid and was created after billing began (no free period)
      suspended  a renewal failed for a week
      ended      it cancelled and the month it paid for is over
    """
    sub = await db.get(Subscription, business_id)
    if sub is None:
        return "required" if must_have_plan(await db.get(Business, business_id)) else None
    if sub.status == "suspended":
        return "suspended"
    return "ended" if is_blocked(sub) else None


async def plan_blocked(db: AsyncSession, business_id: uuid.UUID) -> bool:
    return await plan_state(db, business_id) is not None


async def voice_calls_blocked(db: AsyncSession, business_id: uuid.UUID) -> bool:
    """AI-run calls, in or out, stop when the plan is paused or the wallet that pays for them is empty."""
    return await plan_blocked(db, business_id) or await wallet.voice_blocked(db, business_id)


async def require_active_plan(db: AsyncSession, business_id: uuid.UUID) -> None:
    if await plan_blocked(db, business_id):
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "subscription_suspended",
                "message": "Your plan is paused because the last payment did not go through. Pay on the Billing page to continue.",
            },
        )


async def require_wallet(db: AsyncSession, business_id: uuid.UUID) -> None:
    if await wallet.voice_blocked(db, business_id):
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "wallet_empty",
                "message": "Your wallet is empty or a phone number's rent is unpaid, so calls are off. Add money on the Billing page.",
            },
        )
