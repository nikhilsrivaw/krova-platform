"""
Which customers a team member may see.

The owner and admins see everyone. An agent never sees a private chat (the owner's personal
threads - the AI keeps out of them, and so does the team), and when the owner has chosen
"Their own and unowned chats" (team settings, agent_visibility="assigned") an agent sees only
customers assigned to them or to nobody.

One rule, applied everywhere a customer shows up - the inbox, customers, ledger, approvals,
escalations, leads, CRM, payment links and sends - so a hidden customer cannot be read back
through a side door.
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Business, Customer
from shared.team import settings as team_settings
from shared.team.conflict import SUPERVISORS


def supervisor(user) -> bool:
    return user.role in SUPERVISORS


async def own_only(db: AsyncSession, business_id: uuid.UUID) -> bool:
    """True when the owner chose "Their own and unowned chats" for agents."""
    business = await db.get(Business, business_id)
    return bool(business) and team_settings.read(business)["agent_visibility"] == "assigned"


async def customer_rule(db: AsyncSession, user):
    """A condition on Customer for what this person may see, or None when they see everyone."""
    if supervisor(user):
        return None
    rule = [Customer.is_private.is_(False)]
    if await own_only(db, user.business):
        rule.append(or_(Customer.assigned_to_user_id == user.id, Customer.assigned_to_user_id.is_(None)))
    return and_(*rule)


async def visible_ids(db: AsyncSession, user):
    """
    A subquery of the customer ids this person may see, or None when they see everyone - for
    filtering rows that hang off a customer (promises, drafts, escalations, leads).
    """
    rule = await customer_rule(db, user)
    if rule is None:
        return None
    return select(Customer.id).where(Customer.business_id == user.business, rule)


async def can_see(db: AsyncSession, user, customer: Customer) -> bool:
    if supervisor(user):
        return True
    if customer.is_private:
        return False
    if customer.assigned_to_user_id in (None, user.id):
        return True
    return not await own_only(db, user.business)


async def check(db: AsyncSession, user, customer: Customer) -> None:
    """Raise 403 when this person may not see this customer."""
    if await can_see(db, user, customer):
        return
    if customer.is_private:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This is a private chat.")
    raise HTTPException(status.HTTP_403_FORBIDDEN, "This chat is with a teammate.")


async def check_id(db: AsyncSession, user, customer_id: uuid.UUID | None) -> Customer | None:
    """
    Load a customer of this business and check it - 404 when it is not this business's. A missing
    id (an escalation from an unknown caller) passes through as None.
    """
    if customer_id is None:
        return None
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.business_id != user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")
    await check(db, user, customer)
    return customer


# ── What a team member may do (Team page -> "What agents can see") ─────────


async def permissions(db: AsyncSession, user) -> dict:
    """See shared/team/settings.py::permissions - everything for the owner and admins."""
    business = await db.get(Business, user.business) if not supervisor(user) else None
    return team_settings.permissions(business, user.role)


REFUSED = {
    "see_money": "The owner has kept business totals to the owner and admins.",
    "take_payments": "The owner has kept payments to the owner and admins.",
    "send_templates": "The owner has kept template messages to the owner and admins.",
}


async def require(db: AsyncSession, user, permission: str) -> None:
    """403 when the owner switched this off for team members."""
    if not (await permissions(db, user))[permission]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, REFUSED[permission])


def mask(value: str | None) -> str | None:
    """+919876543210 -> ••••••3210. Enough to tell two customers apart, not enough to dial."""
    if not value:
        return value
    return "••••••" + value[-4:] if len(value) > 4 else "••••"


PHONE_KINDS = ("phone", "whatsapp")


async def hide_phones(db: AsyncSession, user) -> bool:
    return not (await permissions(db, user))["see_phones"]


def identities_out(identities: list[dict], hide: bool) -> list[dict]:
    """Identity dicts ({kind, value}) with phone numbers masked when `hide` - marked so the apps send by customer id."""
    if not hide:
        return identities
    return [
        {**i, "value": mask(i["value"]), "masked": True} if i.get("kind") in PHONE_KINDS else i
        for i in identities
    ]
