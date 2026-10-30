"""
The inbox.

The screen a business actually lives in, and the one thing every competitor
has. Krova stores every message from every channel already; without this
endpoint none of it can be read back.

What makes this list different from a WhatsApp inbox is what sits alongside
each conversation: whether the 24-hour window is still open, and what has been
promised in it. A shared inbox shows you messages. This shows you obligations.

One conversation per customer, not per channel. Someone who emails on Monday
and WhatsApps on Wednesday is one thread here, because they are one person -
which is the entire point of resolving identity across channels.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select

from services.api.dependencies import CurrentUserDep, DbDep, OwnerOrAdminDep
from shared.audit import activity
from shared.team import access, conflict, presence
from shared.channels.whatsapp.client import SERVICE_WINDOW, within_service_window
from shared.db.models import (
    Business,
    BusinessMember,
    Commitment,
    CommitmentStatus,
    ConversationRead,
    Customer,
    CustomerIdentity,
    Direction,
    Message,
)
from shared.team import settings as team_settings

router = APIRouter(prefix="/conversations", tags=["conversations"])


class ConversationSummary(BaseModel):
    customer_id: str
    name: str | None
    identities: list[dict]
    channels: list[str]
    last_message: str | None
    last_message_at: str | None
    last_direction: str | None
    message_count: int
    open_commitments: int
    # Whether a free-form reply will still deliver. Shown in the list because
    # it decides what the business can do next, and it expires silently.
    window_open: bool
    window_closes_at: str | None
    is_private: bool
    assigned_to_user_id: str | None
    # When the customer's unanswered run began (their first message after the last reply), or None if nobody is
    # waiting; and the business's first-reply target in minutes (Team page), so a screen can turn the wait red.
    waiting_since: str | None = None
    wait_target_minutes: int | None = None
    # Marked done by the team and nothing new from the customer since.
    resolved: bool = False
    # Customer messages this person has not read yet (0 once anyone has replied).
    unread: int = 0


class ThreadMessage(BaseModel):
    id: str
    channel: str
    direction: str
    text: str | None
    subject: str | None
    media: dict
    occurred_at: str
    analysed: bool


class Thread(BaseModel):
    customer_id: str
    name: str | None
    identities: list[dict]
    window_open: bool
    window_closes_at: str | None
    is_private: bool
    assigned_to_user_id: str | None
    messages: list[ThreadMessage]
    commitments: list[dict]
    waiting_since: str | None = None
    wait_target_minutes: int | None = None
    resolved: bool = False
    resolved_at: str | None = None


def _value(v) -> str:
    return v.value if hasattr(v, "value") else str(v)


async def _identities(customer_id: uuid.UUID, db: DbDep) -> list[dict]:
    rows = await db.execute(
        select(CustomerIdentity).where(CustomerIdentity.customer_id == customer_id)
    )
    return [{"kind": _value(i.kind), "value": i.value} for i in rows.scalars().all()]


def _aware(at: datetime | None) -> datetime | None:
    if at is not None and at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at


def chat_state(
    *, last_inbound: datetime | None, last_outbound: datetime | None, first_waiting: datetime | None,
    resolved_at: datetime | None,
) -> tuple[str | None, bool]:
    """
    (waiting_since, resolved) for one chat. The customer is waiting when their latest message is newer than the
    business's latest reply; the chat is resolved when the team marked it done and the customer has not written since.
    Pure, so the rule is tested on its own.
    """
    last_inbound, last_outbound, resolved_at = _aware(last_inbound), _aware(last_outbound), _aware(resolved_at)
    resolved = resolved_at is not None and (last_inbound is None or last_inbound <= resolved_at)
    waiting = last_inbound is not None and (last_outbound is None or last_inbound > last_outbound) and not resolved
    since = _aware(first_waiting) or last_inbound
    return (since.isoformat() if waiting and since else None), resolved


async def _wait_target(db, business_id: uuid.UUID) -> int | None:
    business = await db.get(Business, business_id)
    return team_settings.read(business)["sla_minutes"] if business else None


def _window(last_inbound: datetime | None) -> tuple[bool, str | None]:
    if last_inbound is None:
        return False, None
    if last_inbound.tzinfo is None:
        last_inbound = last_inbound.replace(tzinfo=timezone.utc)
    closes = last_inbound + SERVICE_WINDOW
    return within_service_window(last_inbound), closes.isoformat()


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(
    current_user: CurrentUserDep,
    db: DbDep,
    limit: int = Query(default=50, le=200),
    include_private: bool = False,
    # Unlike the frontend's own name/phone/last-message filter (instant,
    # client-side, but only ever sees the one message it already fetched),
    # this searches the actual message content in the database - the only
    # way to find a thread by something said three messages ago.
    q: str | None = Query(default=None, min_length=1, max_length=200),
) -> list[ConversationSummary]:
    """
    Every conversation, most recent first.

    Private customers are excluded by default. The owner marked them private
    precisely so they stop appearing in the working view - and an agent never
    sees them at all (shared/team/access.py).
    """
    business_id = current_user.business

    conditions = [Customer.business_id == business_id]
    rule = await access.customer_rule(db, current_user)
    if rule is not None:
        conditions.append(rule)
    elif not include_private:
        conditions.append(Customer.is_private == False)  # noqa: E712

    if q:
        pattern = f"%{q}%"
        matching_ids = (
            await db.execute(
                select(Message.customer_id)
                .where(Message.business_id == business_id, Message.content.ilike(pattern))
                .distinct()
            )
        ).scalars().all()
        conditions.append(
            (Customer.display_name.ilike(pattern)) | (Customer.id.in_(matching_ids))
        )

    result = await db.execute(
        select(Customer)
        .where(*conditions)
        .order_by(Customer.last_contact_at.desc().nullslast())
        .limit(limit)
    )
    customers = list(result.scalars().all())
    if not customers:
        return []

    ids = [c.id for c in customers]

    # One query for the whole page rather than one per row - a 50-conversation
    # inbox should not be 150 round trips.
    counts = dict(
        (
            await db.execute(
                select(Message.customer_id, func.count(Message.id))
                .where(Message.customer_id.in_(ids))
                .group_by(Message.customer_id)
            )
        ).all()
    )
    open_counts = dict(
        (
            await db.execute(
                select(Commitment.customer_id, func.count(Commitment.id))
                .where(
                    Commitment.customer_id.in_(ids),
                    Commitment.status == CommitmentStatus.open,
                )
                .group_by(Commitment.customer_id)
            )
        ).all()
    )

    hide = await access.hide_phones(db, current_user)
    target = await _wait_target(db, business_id)
    read_at = dict((await db.execute(
        select(ConversationRead.customer_id, ConversationRead.last_read_at).where(
            ConversationRead.user_id == current_user.id, ConversationRead.customer_id.in_(ids))
    )).all())
    out: list[ConversationSummary] = []
    for customer in customers:
        latest = (
            await db.execute(
                select(Message)
                .where(Message.customer_id == customer.id)
                .order_by(Message.occurred_at.desc())
                .limit(1)
            )
        ).scalars().first()

        last_inbound = (
            await db.execute(
                select(Message.occurred_at)
                .where(
                    Message.customer_id == customer.id,
                    Message.direction == Direction.inbound,
                )
                .order_by(Message.occurred_at.desc())
                .limit(1)
            )
        ).scalars().first()

        channels = (
            await db.execute(
                select(Message.channel)
                .where(Message.customer_id == customer.id)
                .distinct()
            )
        ).scalars().all()

        open_now, closes = _window(last_inbound)
        waiting_since, resolved, unread = await _waiting(db, customer, last_inbound, read_at.get(customer.id))

        out.append(
            ConversationSummary(
                customer_id=str(customer.id),
                name=customer.display_name,
                identities=access.identities_out(await _identities(customer.id, db), hide),
                channels=sorted({_value(c) for c in channels}),
                last_message=(latest.content or "")[:160] if latest else None,
                last_message_at=latest.occurred_at.isoformat() if latest else None,
                last_direction=_value(latest.direction) if latest else None,
                message_count=int(counts.get(customer.id, 0)),
                open_commitments=int(open_counts.get(customer.id, 0)),
                window_open=open_now,
                window_closes_at=closes,
                is_private=customer.is_private,
                assigned_to_user_id=(
                    str(customer.assigned_to_user_id) if customer.assigned_to_user_id else None
                ),
                waiting_since=waiting_since,
                wait_target_minutes=target,
                resolved=resolved,
                unread=unread,
            )
        )

    return out


async def _waiting(db, customer: Customer, last_inbound, read_at) -> tuple[str | None, bool, int]:
    """waiting_since, resolved and this person's unread count for one chat (a reply by anyone counts as read)."""
    last_outbound = (await db.execute(
        select(func.max(Message.occurred_at)).where(
            Message.customer_id == customer.id, Message.direction == Direction.outbound)
    )).scalar()
    first_waiting = None
    if last_inbound is not None and (last_outbound is None or _aware(last_inbound) > _aware(last_outbound)):
        after = [Message.customer_id == customer.id, Message.direction == Direction.inbound]
        if last_outbound is not None:
            after.append(Message.occurred_at > last_outbound)
        first_waiting = (await db.execute(select(func.min(Message.occurred_at)).where(*after))).scalar()
    waiting_since, resolved = chat_state(
        last_inbound=last_inbound, last_outbound=last_outbound, first_waiting=first_waiting,
        resolved_at=customer.resolved_at,
    )
    unread = 0
    if waiting_since:
        floor = max(x for x in (_aware(last_outbound), _aware(read_at), _aware(customer.resolved_at),
                                datetime.min.replace(tzinfo=timezone.utc)) if x is not None)
        unread = int((await db.execute(select(func.count(Message.id)).where(
            Message.customer_id == customer.id, Message.direction == Direction.inbound, Message.occurred_at > floor,
        ))).scalar() or 0)
    return waiting_since, resolved, unread


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@router.get("/unread-count")
async def unread_count(current_user: CurrentUserDep, db: DbDep) -> dict:
    """
    How many chats this person may see have customer messages they have not read - the number on the app's Inbox
    tab. A chat counts when the customer wrote after the last reply, after this person last opened it, and after it
    was marked resolved.
    """
    per_customer = (
        select(
            Message.customer_id.label("customer_id"),
            func.max(Message.occurred_at).filter(Message.direction == Direction.inbound).label("last_in"),
            func.max(Message.occurred_at).filter(Message.direction == Direction.outbound).label("last_out"),
        )
        .where(Message.business_id == current_user.business)
        .group_by(Message.customer_id)
        .subquery()
    )
    conditions = [
        Customer.business_id == current_user.business,
        per_customer.c.last_in.is_not(None),
        per_customer.c.last_in > func.coalesce(per_customer.c.last_out, EPOCH),
        per_customer.c.last_in > func.coalesce(ConversationRead.last_read_at, EPOCH),
        per_customer.c.last_in > func.coalesce(Customer.resolved_at, EPOCH),
    ]
    rule = await access.customer_rule(db, current_user)
    conditions.append(rule if rule is not None else Customer.is_private == False)  # noqa: E712
    count = (await db.execute(
        select(func.count(Customer.id))
        .join(per_customer, per_customer.c.customer_id == Customer.id)
        .outerjoin(ConversationRead, (ConversationRead.customer_id == Customer.id) & (ConversationRead.user_id == current_user.id))
        .where(*conditions)
    )).scalar()
    return {"unread_chats": int(count or 0)}


@router.post("/{customer_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(customer_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
    """This person has the chat open: everything in it so far counts as read, for them alone."""
    from sqlalchemy.dialects.postgresql import insert

    customer = await db.get(Customer, customer_id)
    if customer is None or customer.business_id != current_user.business:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    await access.check(db, current_user, customer)
    now = datetime.now(timezone.utc)
    await db.execute(
        insert(ConversationRead)
        .values(id=uuid.uuid4(), business_id=current_user.business, customer_id=customer_id,
                user_id=current_user.id, last_read_at=now)
        .on_conflict_do_update(constraint="uq_conversation_read", set_={"last_read_at": now})
    )


@router.post("/{customer_id}/resolve")
async def resolve_conversation(
    customer_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep, resolved: bool = True,
) -> dict:
    """
    Mark the chat done (or open it again). Anyone who may reply to the customer may do it. A new message from the
    customer reopens it by itself - nothing has to be reset.
    """
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.business_id != current_user.business:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    await access.check(db, current_user, customer)
    if resolved:
        customer.resolved_at = datetime.now(timezone.utc)
        customer.resolved_by_user_id = current_user.id
    else:
        customer.resolved_at = None
        customer.resolved_by_user_id = None
    activity.note(resolved=str(resolved).lower())
    return {"customer_id": str(customer.id), "resolved": resolved,
            "resolved_at": customer.resolved_at.isoformat() if customer.resolved_at else None}


@router.get("/{customer_id}", response_model=Thread)
async def get_thread(
    customer_id: uuid.UUID,
    current_user: CurrentUserDep,
    db: DbDep,
    limit: int = Query(default=200, le=500),
) -> Thread:
    """
    One customer's whole history, across every channel, oldest first.

    Email, WhatsApp and phone calls interleaved on a single timeline - which
    is the thing a business cannot get anywhere else, because everyone else
    owns one channel each.
    """
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.business_id != current_user.business:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )
    await access.check(db, current_user, customer)

    rows = await db.execute(
        select(Message)
        .where(Message.customer_id == customer_id)
        .order_by(Message.occurred_at.desc())
        .limit(limit)
    )
    messages = list(rows.scalars().all())[::-1]

    last_inbound = next(
        (m.occurred_at for m in reversed(messages) if m.direction == Direction.inbound),
        None,
    )
    open_now, closes = _window(last_inbound)
    waiting_since, resolved, _ = await _waiting(db, customer, last_inbound, None)

    commitments = await db.execute(
        select(Commitment)
        .where(Commitment.customer_id == customer_id)
        .order_by(Commitment.due_at.asc().nullslast())
    )

    return Thread(
        customer_id=str(customer.id),
        name=customer.display_name,
        identities=access.identities_out(
            await _identities(customer.id, db), await access.hide_phones(db, current_user)
        ),
        window_open=open_now,
        window_closes_at=closes,
        is_private=customer.is_private,
        assigned_to_user_id=(
            str(customer.assigned_to_user_id) if customer.assigned_to_user_id else None
        ),
        waiting_since=waiting_since,
        wait_target_minutes=await _wait_target(db, current_user.business),
        resolved=resolved,
        resolved_at=customer.resolved_at.isoformat() if customer.resolved_at else None,
        messages=[
            ThreadMessage(
                id=str(m.id),
                channel=_value(m.channel),
                direction=_value(m.direction),
                text=m.content,
                subject=m.subject,
                media=m.media or {},
                occurred_at=m.occurred_at.isoformat(),
                analysed=m.analysed_at is not None,
            )
            for m in messages
        ],
        commitments=[
            {
                "id": str(c.id),
                "direction": _value(c.direction),
                "description": c.description,
                "amount_paise": c.amount_paise,
                "due_at": c.due_at.isoformat() if c.due_at else None,
                "status": _value(c.status),
            }
            for c in commitments.scalars().all()
        ],
    )


@router.post("/{customer_id}/private")
async def set_private(
    customer_id: uuid.UUID, current_user: OwnerOrAdminDep, db: DbDep, private: bool = True
) -> dict:
    """
    Mark a conversation private.

    The agent never reads or answers a private customer, and nothing is
    derived from their messages. This is what makes "that thread is personal"
    a guarantee rather than a promise - and it is why reading a mixed inbox is
    defensible at all. Owner and admins only: a private chat is hidden from the
    rest of the team, so an agent could otherwise hide a chat from the owner.
    """
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.business_id != current_user.business:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )
    customer.is_private = private
    return {"customer_id": str(customer.id), "is_private": private}


@router.post("/{customer_id}/assign")
async def assign_conversation(
    customer_id: uuid.UUID,
    current_user: CurrentUserDep,
    db: DbDep,
    user_id: str | None = None,
) -> dict:
    """
    Assign a conversation to a teammate, or clear it with user_id omitted.

    The target must actually be on this business's team - assigning to an
    arbitrary user_id would silently create a reference nothing else in the
    product can resolve, and would leak who else uses Krova.
    """
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.business_id != current_user.business:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )
    await access.check(db, current_user, customer)

    if user_id is None:
        customer.assigned_to_user_id = None
        customer.assigned_at = None
        return {"customer_id": str(customer.id), "assigned_to_user_id": None}

    target_id = uuid.UUID(user_id)
    member = await db.execute(
        select(BusinessMember).where(
            BusinessMember.business_id == current_user.business,
            BusinessMember.user_id == target_id,
        )
    )
    if member.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="That person is not on this team"
        )

    customer.assigned_to_user_id = target_id
    customer.assigned_at = datetime.now(timezone.utc)
    activity.note(assigned_to=str(target_id))
    if target_id != current_user.id:
        await conflict.notify(
            db, business_id=current_user.business, user_id=target_id,
            title="A chat was assigned to you", body=customer.display_name or "A customer",
            url=f"/app/inbox/{customer.id}", kind="new_chat",
        )
    return {"customer_id": str(customer.id), "assigned_to_user_id": str(target_id)}


@router.post("/{customer_id}/take-over")
async def take_over_conversation(
    customer_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep
) -> dict:
    """
    Make this chat yours, whoever had it. The one-tap answer to "this chat is with
    Rahul" - logged, and the previous owner is told on their next look at it.
    """
    await access.check_id(db, current_user, customer_id)
    previous = await conflict.take_over(
        db, business_id=current_user.business, customer_id=customer_id, actor_id=current_user.id
    )
    if previous and previous != current_user.id:
        await conflict.notify(
            db, business_id=current_user.business, user_id=previous,
            title="A teammate took over a chat", body="It is no longer yours.", url=f"/app/inbox/{customer_id}",
            kind="new_chat",
        )
    return {
        "customer_id": str(customer_id),
        "assigned_to_user_id": str(current_user.id),
        "previous_user_id": str(previous) if previous else None,
    }


class PresenceIn(BaseModel):
    typing: bool = False
    # True when the person closes the thread, so their name goes away at once.
    left: bool = False


@router.post("/{customer_id}/presence")
async def thread_presence(
    customer_id: uuid.UUID, body: PresenceIn, current_user: CurrentUserDep, db: DbDep
) -> dict:
    """
    Heartbeat from an open thread: "I am here" (and whether I am typing). Returns
    the teammates who are here too, so the UI can say "Rahul is replying".
    Advisory only; the hard guard is on sending.
    """
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    await access.check(db, current_user, customer)
    if body.left:
        await presence.leave(
            db, business_id=current_user.business, customer_id=customer_id, user_id=current_user.id
        )
        return {"viewers": []}
    await presence.touch(
        db, business_id=current_user.business, customer_id=customer_id,
        user_id=current_user.id, typing=body.typing,
    )
    viewers = await presence.viewers(
        db, business_id=current_user.business, customer_id=customer_id,
        exclude_user_id=current_user.id,
    )
    return {"viewers": viewers}
