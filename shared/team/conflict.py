"""
Keeping two teammates from answering the same customer.

The rule, in one line: **the first agent to reply to an unassigned chat owns
it, and other agents are stopped from replying until they take it over.**

  * A chat nobody owns is claimed by the first agent who replies, in a single
    conditional UPDATE - two agents replying in the same instant cannot both win
    because only one UPDATE finds the row still unassigned.
  * An agent replying to a chat a teammate owns is refused (HTTP 409 with the
    owner's name), and can take it over in one tap. Taking over is logged.
  * The owner and admins supervise: they may reply anywhere without taking the
    chat, so stepping in never locks the agent out. It is noted in the log.
  * A business with a single person has no one to collide with, so nothing is
    claimed and nothing is ever refused.

The same shape guards escalations and draft approvals (see the routes that call
claim_escalation and the row lock in routers/approvals.py).
"""

import uuid
from datetime import datetime, timezone
from enum import Enum

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit import activity
from shared.db.models import BusinessMember, Customer, Escalation, User

SUPERVISORS = frozenset({"owner", "admin"})


class Verdict(str, Enum):
    claim = "claim"        # nobody owns it: this person takes it
    mine = "mine"          # already theirs
    override = "override"  # a supervisor stepping into a teammate's chat
    refuse = "refuse"      # an agent in a teammate's chat


def decide(assignee_id: uuid.UUID | None, actor_id: uuid.UUID, actor_role: str | None) -> Verdict:
    """Pure decision, kept separate so the rule can be tested without a database."""
    if assignee_id is None:
        return Verdict.claim if actor_role not in SUPERVISORS else Verdict.mine
    if assignee_id == actor_id:
        return Verdict.mine
    if actor_role in SUPERVISORS:
        return Verdict.override
    return Verdict.refuse


class AssignedToOther(Exception):
    def __init__(self, assignee_id: uuid.UUID, assignee_name: str, assigned_at: datetime | None):
        super().__init__(f"This chat is with {assignee_name}")
        self.assignee_id = assignee_id
        self.assignee_name = assignee_name
        self.assigned_at = assigned_at


def to_http(exc: AssignedToOther, what: str = "chat") -> HTTPException:
    """409 with a structured body the apps turn into a 'Take over' prompt."""
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "code": "assigned_to_other",
            "message": f"This {what} is with {exc.assignee_name}.",
            "assignee_id": str(exc.assignee_id),
            "assignee_name": exc.assignee_name,
            "assigned_at": exc.assigned_at.isoformat() if exc.assigned_at else None,
        },
    )


async def member_count(business_id: uuid.UUID, db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count(BusinessMember.user_id)).where(BusinessMember.business_id == business_id)
    )
    return int(result.scalar_one())


async def _auto_assign_on(business_id: uuid.UUID, db: AsyncSession) -> bool:
    from shared.db.models import Business
    from shared.team import settings as team_settings

    business = await db.get(Business, business_id)
    return team_settings.read(business)["auto_assign_on_reply"] if business else True


async def notify(
    db: AsyncSession, *, business_id: uuid.UUID, user_id: uuid.UUID | None, title: str, body: str, url: str
) -> None:
    """Best-effort push to one person. Never raises: a missed ping must not undo the action."""
    if user_id is None:
        return
    try:
        from shared.integrations import web_push

        await web_push.send_to_users(
            db, business_id=business_id, user_ids=[user_id],
            payload={"title": title, "body": body[:140], "url": url},
        )
    except Exception:
        pass


async def _name_of(user_id: uuid.UUID, db: AsyncSession) -> str:
    user = await db.get(User, user_id)
    if user is None:
        return "a teammate"
    return user.full_name or user.username or user.email or "a teammate"


async def guard_reply(
    db: AsyncSession, *, business_id: uuid.UUID, customer_id: uuid.UUID,
    actor_id: uuid.UUID, actor_role: str | None,
) -> Verdict:
    """
    Call before sending a human reply to this customer. Claims an unowned chat
    for an agent, lets owners/admins through, and raises AssignedToOther when an
    agent would be answering a teammate's customer.
    """
    if await member_count(business_id, db) <= 1:
        return Verdict.mine

    customer = await db.get(Customer, customer_id)
    if customer is None or customer.business_id != business_id:
        return Verdict.mine  # not ours to judge; the send path reports a missing customer

    verdict = decide(customer.assigned_to_user_id, actor_id, actor_role)

    if verdict is Verdict.claim and not await _auto_assign_on(business_id, db):
        return Verdict.mine  # the business turned first-reply ownership off

    if verdict is Verdict.claim:
        now = datetime.now(timezone.utc)
        claimed = await db.execute(
            update(Customer)
            .where(
                Customer.id == customer_id,
                Customer.business_id == business_id,
                Customer.assigned_to_user_id.is_(None),
            )
            .values(assigned_to_user_id=actor_id, assigned_at=now)
            .returning(Customer.id)
        )
        if claimed.first() is not None:
            activity.note(claimed_chat=True)
            return Verdict.claim
        # Someone else won between the read and the write - judge again against the winner.
        await db.refresh(customer)
        verdict = decide(customer.assigned_to_user_id, actor_id, actor_role)

    if verdict is Verdict.refuse:
        assignee = customer.assigned_to_user_id
        raise AssignedToOther(assignee, await _name_of(assignee, db), customer.assigned_at)

    if verdict is Verdict.override:
        activity.note(replied_in_chat_of=str(customer.assigned_to_user_id))
    return verdict


async def guard_reply_to(
    db: AsyncSession, *, business_id: uuid.UUID, kind: str, value: str,
    actor_id: uuid.UUID, actor_role: str | None,
) -> Verdict | None:
    """guard_reply for a send addressed by phone number or Instagram id. None = we have no such customer yet."""
    from shared.db.models import CustomerIdentity

    row = await db.execute(
        select(CustomerIdentity.customer_id).where(
            CustomerIdentity.business_id == business_id,
            CustomerIdentity.kind == kind,
            CustomerIdentity.value == value,
        ).limit(1)
    )
    customer_id = row.scalar_one_or_none()
    if customer_id is None:
        return None
    return await guard_reply(
        db, business_id=business_id, customer_id=customer_id,
        actor_id=actor_id, actor_role=actor_role,
    )


async def take_over(
    db: AsyncSession, *, business_id: uuid.UUID, customer_id: uuid.UUID, actor_id: uuid.UUID,
) -> uuid.UUID | None:
    """Make this person the owner of the chat. Returns who had it before (None if nobody)."""
    customer = await db.get(Customer, customer_id, with_for_update=True)
    if customer is None or customer.business_id != business_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    previous = customer.assigned_to_user_id
    customer.assigned_to_user_id = actor_id
    customer.assigned_at = datetime.now(timezone.utc)
    activity.note(took_over_from=str(previous) if previous else None)
    return previous


# ── Escalations ──────────────────────────────────────────────────────────────

async def claim_escalation(
    db: AsyncSession, *, business_id: uuid.UUID, escalation_id: uuid.UUID,
    actor_id: uuid.UUID, actor_role: str | None, force: bool = False,
) -> Escalation:
    """
    Take an escalation. One person holds it; a second agent is refused with the
    holder's name. `force` (owner/admin only) reassigns it deliberately.
    """
    escalation = await db.get(Escalation, escalation_id)
    if escalation is None or escalation.business_id != business_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Escalation not found")
    if escalation.status in ("resolved", "dismissed"):
        raise HTTPException(status.HTTP_409_CONFLICT, "This escalation is already closed.")

    now = datetime.now(timezone.utc)
    if force and actor_role in SUPERVISORS:
        escalation.assigned_to_user_id = actor_id
        escalation.assigned_at = now
        _ack(escalation, actor_id, now)
        activity.note(reassigned=True)
        return escalation

    won = await db.execute(
        update(Escalation)
        .where(Escalation.id == escalation_id, Escalation.assigned_to_user_id.is_(None))
        .values(assigned_to_user_id=actor_id, assigned_at=now)
        .returning(Escalation.id)
    )
    await db.refresh(escalation)
    if won.first() is not None or escalation.assigned_to_user_id == actor_id:
        _ack(escalation, actor_id, now)
        return escalation

    holder = escalation.assigned_to_user_id
    raise to_http(
        AssignedToOther(holder, await _name_of(holder, db), escalation.assigned_at), "escalation"
    )


def _ack(escalation: Escalation, actor_id: uuid.UUID, now: datetime) -> None:
    if escalation.acknowledged_at is None:
        escalation.acknowledged_at = now
        escalation.acknowledged_by_user_id = actor_id
    if escalation.status == "open":
        escalation.status = "in_progress"


async def guard_escalation_change(
    db: AsyncSession, *, escalation: Escalation, actor_id: uuid.UUID, actor_role: str | None,
) -> None:
    """
    Before changing an escalation's status: an agent can't resolve a teammate's;
    an unowned one is claimed by whoever acts on it; supervisors may always act.
    """
    if await member_count(escalation.business_id, db) <= 1:
        return
    verdict = decide(escalation.assigned_to_user_id, actor_id, actor_role)
    if verdict is Verdict.refuse:
        holder = escalation.assigned_to_user_id
        raise to_http(
            AssignedToOther(holder, await _name_of(holder, db), escalation.assigned_at), "escalation"
        )
    if verdict is Verdict.claim:
        won = await db.execute(
            update(Escalation)
            .where(Escalation.id == escalation.id, Escalation.assigned_to_user_id.is_(None))
            .values(assigned_to_user_id=actor_id, assigned_at=datetime.now(timezone.utc))
            .returning(Escalation.id)
        )
        if won.first() is None:
            await db.refresh(escalation)
            if escalation.assigned_to_user_id != actor_id:
                holder = escalation.assigned_to_user_id
                raise to_http(
                    AssignedToOther(holder, await _name_of(holder, db), escalation.assigned_at),
                    "escalation",
                )
        else:
            await db.refresh(escalation)
