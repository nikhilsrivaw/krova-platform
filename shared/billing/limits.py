"""
What each plan can hold. One table, read by every place that has a limit (and mirrored in
krova-web/lib/planGuide.ts for the pricing page). Plain numbers - no database, no network.

Only *capacity* differs between plans. Features (Ask KROVA, reply modes, automations, analytics,
voice call campaigns, team tools, support) are the same on all three, on purpose.

A business whose plan is not one of these (a trial, or one from before billing) has no plan limits
and is held only by the platform-wide ceilings that were already there.
"""

import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class PlanLimits:
    messages: int          # customer messages a month the plan is sized for (shown, not enforced)
    phone_lines: int
    team_logins: int
    knowledge_tokens: int  # what the AI reads before every reply
    campaign_audience: int # people in one WhatsApp / Instagram campaign
    import_rows: int       # contacts in one bulk import
    lead_sources: int | None  # connected lead sources; None = all
    api_keys: int
    webhooks: int


PLAN_LIMITS: dict[str, PlanLimits] = {
    "starter": PlanLimits(1_000, 1, 3, 6_000, 500, 5_000, 1, 0, 0),
    "pro": PlanLimits(2_000, 3, 8, 10_000, 3_000, 20_000, None, 2, 5),
    "scale": PlanLimits(3_500, 5, 20, 16_000, 10_000, 50_000, None, 10, 20),
}

PLAN_NAMES = {"starter": "Starter", "pro": "Pro", "scale": "Scale"}
_NEXT = {"starter": "Pro", "pro": "Scale"}


def limits_for(plan: str | None) -> PlanLimits | None:
    return PLAN_LIMITS.get((plan or "").lower())


def _over(plan: str, what: str, limit: int) -> HTTPException:
    nxt = _NEXT.get(plan)
    tail = f" Move to {nxt} on the Billing page for more." if nxt else ""
    if limit == 0:
        text = f"{what} are not part of the {PLAN_NAMES[plan]} plan.{tail}"
    else:
        text = f"Your {PLAN_NAMES[plan]} plan includes {limit} {what}.{tail}"
    return HTTPException(status.HTTP_402_PAYMENT_REQUIRED, detail={"code": "plan_limit", "message": text})


def check_count(plan: str | None, field: str, label: str, current: int) -> None:
    """Raise 402 when adding one more would pass the plan's limit for `field`."""
    lim = limits_for(plan)
    if lim is None:
        return
    cap = getattr(lim, field)
    if cap is None:
        return
    if current >= cap:
        raise _over(plan.lower(), label, cap)


def check_size(plan: str | None, field: str, label: str, size: int) -> None:
    """Raise 402 when `size` (people, rows, tokens) is over the plan's limit for `field`."""
    lim = limits_for(plan)
    if lim is None:
        return
    cap = getattr(lim, field)
    if cap is not None and size > cap:
        raise _over(plan.lower(), label, cap)


# ── Counters ──────────────────────────────────────────────────────────────

# Settings keys that mean "this lead source is connected" - one per source.
LEAD_SOURCE_KEYS = (
    "justdial_token_hash",
    "indiamart_token_hash",
    "email_leads_token_hash",
    "magicbricks_token_hash",
    "99acres_token_hash",
    "housing_token_hash",
    "generic_token_hash",
)


def connected_lead_sources(settings_map: dict | None) -> list[str]:
    return [k for k in LEAD_SOURCE_KEYS if (settings_map or {}).get(k)]


def check_new_lead_source(plan: str | None, settings_map: dict | None, key: str) -> None:
    """Connecting a source it already has is not a new one."""
    if key in connected_lead_sources(settings_map):
        return
    check_count(plan, "lead_sources", "connected lead source(s)", len(connected_lead_sources(settings_map)))


async def count_rows(db: AsyncSession, model, *where) -> int:
    return int((await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one())


async def messages_this_month(db: AsyncSession, business_id: uuid.UUID) -> int:
    """Customer messages received since the 1st of this month (UTC)."""
    from datetime import datetime, timezone

    from shared.db.models import UsageEvent, UsageEventType

    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = (
        await db.execute(
            select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
                UsageEvent.business_id == business_id,
                UsageEvent.event_type == UsageEventType.message_processed,
                UsageEvent.occurred_at >= start,
            )
        )
    ).scalar_one()
    return int(total)
