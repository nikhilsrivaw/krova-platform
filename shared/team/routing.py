"""
Round-robin: a new customer's chat goes to the available agent who was handed
one longest ago, so work spreads evenly and an "Away" person gets none.

Runs when an inbound message lands for a chat nobody owns, only if the business
turned routing on. Nobody available -> the chat stays unowned and everyone is
pinged as before. The claim is one conditional UPDATE, so two messages arriving
together cannot give the same chat to two people.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Business, BusinessMember, BusinessRole, Customer, User
from shared.team import conflict
from shared.team import settings as team_settings


async def pick_agent(db: AsyncSession, business_id: uuid.UUID) -> BusinessMember | None:
    rows = await db.execute(
        select(BusinessMember)
        .join(User, User.id == BusinessMember.user_id)
        .where(
            BusinessMember.business_id == business_id,
            BusinessMember.role == BusinessRole.agent,
            BusinessMember.available == True,  # noqa: E712
            User.is_active == True,  # noqa: E712
        )
        .order_by(BusinessMember.last_routed_at.asc().nullsfirst(), BusinessMember.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True, of=BusinessMember)
    )
    return rows.scalar_one_or_none()


async def assign(
    db: AsyncSession, *, business_id: uuid.UUID, customer_id: uuid.UUID, user_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """
    Give an unowned chat to a named team member, or (user_id None) to the next available
    agent whatever the business's routing setting says - an automation asked for it
    explicitly. Returns who got it, or None (nobody available, not on the team, or
    somebody took it first).
    """
    if user_id is None:
        member = await pick_agent(db, business_id)
    else:
        member = (await db.execute(
            select(BusinessMember).where(
                BusinessMember.business_id == business_id, BusinessMember.user_id == user_id,
            )
        )).scalar_one_or_none()
    if member is None:
        return None

    now = datetime.now(timezone.utc)
    won = await db.execute(
        update(Customer)
        .where(
            Customer.id == customer_id, Customer.business_id == business_id,
            Customer.assigned_to_user_id.is_(None),
        )
        .values(assigned_to_user_id=member.user_id, assigned_at=now)
        .returning(Customer.id)
    )
    if won.first() is None:
        return None
    member.last_routed_at = now
    customer = await db.get(Customer, customer_id)
    await conflict.notify(
        db, business_id=business_id, user_id=member.user_id,
        title="Chat assigned to you", body=(customer.display_name if customer else None) or "A customer",
        url=f"/app/inbox/{customer_id}",
    )
    return member.user_id


async def route_new_chat(db: AsyncSession, *, business_id: uuid.UUID, customer_id: uuid.UUID) -> uuid.UUID | None:
    """Hand an unowned chat to the next available agent. Returns who got it, or None."""
    business = await db.get(Business, business_id)
    if business is None or team_settings.read(business)["routing"] != "round_robin":
        return None

    member = await pick_agent(db, business_id)
    if member is None:
        return None

    now = datetime.now(timezone.utc)
    won = await db.execute(
        update(Customer)
        .where(
            Customer.id == customer_id, Customer.business_id == business_id,
            Customer.assigned_to_user_id.is_(None),
        )
        .values(assigned_to_user_id=member.user_id, assigned_at=now)
        .returning(Customer.id)
    )
    if won.first() is None:
        return None  # somebody took it first
    member.last_routed_at = now

    customer = await db.get(Customer, customer_id)
    await conflict.notify(
        db, business_id=business_id, user_id=member.user_id,
        title="New chat for you", body=(customer.display_name if customer else None) or "A customer wrote in",
        url=f"/app/inbox/{customer_id}",
    )
    return member.user_id
