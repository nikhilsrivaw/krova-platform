"""
Calls a team member makes from the app, and writing them up afterwards.

Start: the member taps Call on a customer. We check everything a message send checks
(an active plan, a teammate's customer is not stolen, a paid-up wallet) plus what a call
needs (the member's own phone, the customer's phone, a connected voice number), then ring
the member's phone first. The webhooks that carry on from there live in
shared/channels/voice/human_calls.py.

Wrap up: the point of a call is what was said. After it, the member writes one line and any
promises they made ("I'll send the quote tomorrow"). The line becomes a team note and a
timeline entry; each promise goes straight into the Commitment Ledger, owned by the customer
and traceable to that entry - so a promise made on a phone is chased like any other.
"""

import uuid
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt
from shared.billing.guards import require_active_plan, require_wallet
from shared.channels import ingest
from shared.channels.voice import plivo_client
from shared.config.settings import settings
from shared.db.models import (
    BusinessMember,
    Call,
    Channel,
    ChannelConnection,
    Commitment,
    CommitmentDirection,
    CommitmentKind,
    CommitmentStatus,
    ConnectionStatus,
    Customer,
    CustomerIdentity,
    CustomerNote,
    Direction,
    IdentityKind,
    User,
)
from shared.team import conflict
from shared.utils.logging import get_logger

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
MAX_PROMISES = 5
DUE_HOUR = 18  # a promise with a day but no time is due at the end of that working day
WRAPUP_WINDOW = timedelta(days=7)


def _fail(code: str, message: str, http: int = status.HTTP_409_CONFLICT) -> HTTPException:
    return HTTPException(http, detail={"code": code, "message": message})


async def _customer_phone(db: AsyncSession, customer_id: uuid.UUID) -> str | None:
    row = await db.execute(
        select(CustomerIdentity.value).where(
            CustomerIdentity.customer_id == customer_id, CustomerIdentity.kind == IdentityKind.phone,
        ).limit(1)
    )
    return row.scalar_one_or_none()


async def start(
    db: AsyncSession, *, business_id: uuid.UUID, user_id: uuid.UUID, role: str | None, customer_id: uuid.UUID,
) -> Call:
    """Ring the member's phone; the customer is dialled once they pick up. Returns the Call row."""
    member = (await db.execute(
        select(BusinessMember).where(
            BusinessMember.business_id == business_id, BusinessMember.user_id == user_id)
    )).scalar_one_or_none()
    if member is None:
        raise _fail("not_on_team", "You are not on this team.", status.HTTP_403_FORBIDDEN)
    if not member.phone:
        raise _fail("no_member_phone", "Add your own phone number first - that is where the call rings.")

    customer = await db.get(Customer, customer_id)
    if customer is None or customer.business_id != business_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")
    if customer.is_private and role not in ("owner", "admin"):
        raise _fail("private", "This is a private chat.", status.HTTP_403_FORBIDDEN)
    number = await _customer_phone(db, customer_id)
    if not number:
        raise _fail("no_customer_phone", "This customer has no phone number on file.")

    await require_active_plan(db, business_id)
    await require_wallet(db, business_id)

    connection = (await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == business_id, ChannelConnection.channel == Channel.voice,
            ChannelConnection.status == ConnectionStatus.active,
        )
    )).scalars().first()
    auth_id = (connection.extra or {}).get("subaccount_auth_id") if connection else None
    if connection is None or not connection.access_token or not auth_id:
        raise _fail("no_voice_number", "Connect a phone number on the Voice page first.")

    recent = (await db.execute(
        select(Call.id).where(
            Call.handled_by_user_id == user_id, Call.ended_at.is_(None),
            Call.started_at > datetime.now(timezone.utc) - timedelta(seconds=90),
            Call.direction == Direction.outbound,
        ).limit(1)
    )).scalar_one_or_none()
    if recent is not None:
        raise _fail("already_calling", "A call is already being connected. Pick up your phone.")

    # Same rule as replying: a teammate's customer is theirs, an unowned one becomes mine.
    try:
        await conflict.guard_reply(
            db, business_id=business_id, customer_id=customer_id, actor_id=user_id, actor_role=role,
        )
    except conflict.AssignedToOther as exc:
        raise conflict.to_http(exc, "customer") from exc

    now = datetime.now(timezone.utc)
    call = Call(
        business_id=business_id, connection_id=connection.id, customer_id=customer_id,
        direction=Direction.outbound, external_id=f"pending-{uuid.uuid4()}", transport="plivo",
        ring_started_at=now, started_at=now, handled_by_user_id=user_id,
        cost_breakdown={"human": True},
    )
    db.add(call)
    await db.flush()
    await db.commit()

    base = settings.public_base_url.rstrip("/")
    query = urlencode({"call": str(call.id)})
    try:
        await plivo_client.make_call(
            auth_id=auth_id, auth_token=decrypt(connection.access_token),
            from_number=connection.external_account_id, to_number=member.phone,
            answer_url=f"{base}/voice/bridge-answer?{query}",
            hangup_url=f"{base}/voice/bridge-hangup?{query}",
        )
    except plivo_client.PlivoError as exc:
        logger.warning("click-to-call failed to place business=%s: %s", business_id, exc)
        call.ended_at = datetime.now(timezone.utc)
        call.hangup_cause = "failed_to_place"
        call.wrapped_up_at = call.ended_at
        await db.commit()
        raise _fail("could_not_place", "The call could not be placed. Try again in a moment.", status.HTTP_502_BAD_GATEWAY) from exc
    return call


def state_of(call: Call) -> str:
    if call.ended_at is not None:
        return "ended"
    return "talking" if call.answered_at is not None else "ringing_you"


async def pending_wrapups(db: AsyncSession, *, business_id: uuid.UUID, user_id: uuid.UUID) -> list[Call]:
    """Calls this person spoke on and has not written up."""
    rows = await db.execute(
        select(Call).where(
            Call.business_id == business_id, Call.handled_by_user_id == user_id,
            Call.ended_at.is_not(None), Call.wrapped_up_at.is_(None),
            Call.started_at > datetime.now(timezone.utc) - WRAPUP_WINDOW,
        ).order_by(Call.ended_at.desc()).limit(20)
    )
    return list(rows.scalars().all())


def _due(on: date | None) -> datetime | None:
    if on is None:
        return None
    return datetime.combine(on, time(DUE_HOUR, 0), tzinfo=IST).astimezone(timezone.utc)


async def wrap_up(
    db: AsyncSession, *, call: Call, user: User, note: str, promises: list[dict],
) -> int:
    """
    Record what was said. Returns how many promises were filed.

    Idempotent: a call already written up is refused, so a double tap cannot file a promise twice.
    """
    if call.ended_at is None:
        raise _fail("call_in_progress", "The call is still going.")
    if call.wrapped_up_at is not None:
        raise _fail("already_wrapped_up", "This call is already written up.")

    note = (note or "").strip()
    cleaned = [
        {**p, "description": (p.get("description") or "").strip()}
        for p in promises[:MAX_PROMISES] if (p.get("description") or "").strip()
    ]
    minutes = max(1, round((call.duration_seconds or 0) / 60)) if call.duration_seconds else 0
    who = user.full_name or user.username or "a teammate"
    customer = await db.get(Customer, call.customer_id) if call.customer_id else None

    message_id = None
    if customer is not None and (note or cleaned):
        phone = await _customer_phone(db, customer.id)
        if phone:
            text = f"Call with {who}" + (f" ({minutes} min)" if minutes else "") + (f": {note}" if note else "")
            result = await ingest.ingest(
                business_id=call.business_id, channel=Channel.voice, direction=Direction.outbound,
                identity_kind=IdentityKind.phone, identity_value=phone, external_id=f"humancall:{call.id}",
                text=text, occurred_at=datetime.now(timezone.utc), db=db, connection_id=call.connection_id,
                enqueue_analysis=False, sent_by_user_id=user.id,
            )
            message_id = result.message.id if result.message is not None else None
        if note:
            db.add(CustomerNote(
                business_id=call.business_id, customer_id=customer.id, author_user_id=user.id,
                body=f"Call ({minutes} min): {note}"[:2000] if minutes else f"Call: {note}"[:2000],
            ))

    filed = 0
    if customer is not None:
        for promise in cleaned:
            kind = promise.get("kind")
            db.add(Commitment(
                business_id=call.business_id, customer_id=customer.id,
                direction=CommitmentDirection.we_owe,
                kind=CommitmentKind(kind) if kind in CommitmentKind._value2member_map_ else CommitmentKind.other,
                description=promise["description"][:500], due_at=_due(promise.get("due_on")),
                due_at_explicit=promise.get("due_on") is not None, status=CommitmentStatus.open,
                confidence=1.0, source_message_ids=[message_id] if message_id else [],
                source_quote=(note or promise["description"])[:300], confirmed_by_user_id=user.id,
            ))
            filed += 1

    call.wrapped_up_at = datetime.now(timezone.utc)
    call.wrapup_note = note or None
    return filed
