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

from services.api.dependencies import CurrentUserDep, DbDep
from shared.channels.whatsapp.client import SERVICE_WINDOW, within_service_window
from shared.db.models import (
    Commitment,
    CommitmentStatus,
    Customer,
    CustomerIdentity,
    Direction,
    Message,
)

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
    messages: list[ThreadMessage]
    commitments: list[dict]


def _value(v) -> str:
    return v.value if hasattr(v, "value") else str(v)


async def _identities(customer_id: uuid.UUID, db: DbDep) -> list[dict]:
    rows = await db.execute(
        select(CustomerIdentity).where(CustomerIdentity.customer_id == customer_id)
    )
    return [{"kind": _value(i.kind), "value": i.value} for i in rows.scalars().all()]


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
) -> list[ConversationSummary]:
    """
    Every conversation, most recent first.

    Private customers are excluded by default. The owner marked them private
    precisely so they stop appearing in the working view.
    """
    business_id = current_user.business

    conditions = [Customer.business_id == business_id]
    if not include_private:
        conditions.append(Customer.is_private == False)  # noqa: E712

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

        out.append(
            ConversationSummary(
                customer_id=str(customer.id),
                name=customer.display_name,
                identities=await _identities(customer.id, db),
                channels=sorted({_value(c) for c in channels}),
                last_message=(latest.content or "")[:160] if latest else None,
                last_message_at=latest.occurred_at.isoformat() if latest else None,
                last_direction=_value(latest.direction) if latest else None,
                message_count=int(counts.get(customer.id, 0)),
                open_commitments=int(open_counts.get(customer.id, 0)),
                window_open=open_now,
                window_closes_at=closes,
                is_private=customer.is_private,
            )
        )

    return out


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

    commitments = await db.execute(
        select(Commitment)
        .where(Commitment.customer_id == customer_id)
        .order_by(Commitment.due_at.asc().nullslast())
    )

    return Thread(
        customer_id=str(customer.id),
        name=customer.display_name,
        identities=await _identities(customer.id, db),
        window_open=open_now,
        window_closes_at=closes,
        is_private=customer.is_private,
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
    customer_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep, private: bool = True
) -> dict:
    """
    Mark a conversation private.

    The agent never reads or answers a private customer, and nothing is
    derived from their messages. This is what makes "that thread is personal"
    a guarantee rather than a promise - and it is why reading a mixed inbox is
    defensible at all.
    """
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.business_id != current_user.business:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )
    customer.is_private = private
    return {"customer_id": str(customer.id), "is_private": private}
