"""
Generating a reply to a website widget message, synchronously - the widget
waits for this in the same HTTP request/response, so this mirrors voice's
shared/channels/voice/pipeline.py::_reply() (generate an immediate answer,
act on any booking, log the turn afterward), never WhatsApp/Instagram's
async draft-queue flow, which is built for a channel nobody is watching
live.

Tier 1 (session.customer_id is None) and Tier 2 (resolved) genuinely
differ in where conversation history lives: Tier 1 has no Customer to
attach a Message to, so turns are appended to WebSession.transcript and
shared/ai/context.py's build_anonymous() reads from that; Tier 2 uses the
exact same ingest()/build() path every other channel does. promote_session()
is the bridge - the moment a visitor gives contact info, it resolves a
real Customer and backfills the transcript into real Message rows (with
their real historical timestamps) so the conversation the visitor already
had is not silently forgotten the instant they identify themselves.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from shared.ai import agent as agent_module
from shared.ai import context as agent_context
from shared.channels import ingest as ingest_module
from shared.db.models import (
    Business,
    Channel,
    Customer,
    Direction,
    IdentityKind,
    IntakeChannel,
    WebSession,
)
from shared.identity import resolver
from shared.scheduling import booking as scheduling_booking
from shared.scheduling import queue_booking
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Mirrors shared/ai/context.py's RECENT_TURNS for the same reason - enough
# for the model to stay coherent, small enough to keep the prompt cheap.
_MAX_TRANSCRIPT_TURNS = 20

_NEEDS_CONTACT_MESSAGE = (
    "I'd love to help you book that - could you share your phone number or "
    "email first so I can confirm it for you?"
)


@dataclass(slots=True)
class ReplyOutcome:
    reply_text: str
    booked: bool
    # True when the model tried to book something but this session has no
    # resolved identity yet - the widget's own UI should show a contact
    # form next, not treat this as a normal answered/escalated turn.
    needs_contact: bool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _append_transcript(session: WebSession, *, role: str, text: str) -> None:
    entry = {"role": role, "text": text, "at": _now().isoformat()}
    session.transcript = (session.transcript or []) + [entry]
    if len(session.transcript) > _MAX_TRANSCRIPT_TURNS:
        session.transcript = session.transcript[-_MAX_TRANSCRIPT_TURNS:]


def _transcript_as_recent(session: WebSession) -> list[dict]:
    # Matches AgentContext.recent's own shape (channel/direction/text) -
    # see shared/ai/context.py's build().
    return [
        {
            "channel": "web",
            "direction": "inbound" if e["role"] == "user" else "outbound",
            "text": e["text"],
        }
        for e in (session.transcript or [])
    ]


async def promote_session(
    db: AsyncSession, *, business: Business, session: WebSession,
    display_name: str | None, phone: str | None, email: str | None,
) -> Customer:
    """
    Resolve this session's first real identity and backfill its anonymous
    transcript into real Message rows, so the conversation already had is
    not lost the instant a visitor identifies themselves. Idempotent -
    returns the existing customer unchanged if already promoted.
    """
    if session.customer_id is not None:
        existing = await db.get(Customer, session.customer_id)
        if existing is not None:
            return existing

    if phone:
        resolution = await resolver.resolve(
            business.id, IdentityKind.phone, phone, db, display_name=display_name
        )
    elif email:
        resolution = await resolver.resolve(
            business.id, IdentityKind.email, email, db, display_name=display_name
        )
    else:
        raise ValueError("promote_session needs a phone or email")

    session.customer_id = resolution.customer.id

    for entry in session.transcript or []:
        direction = Direction.inbound if entry["role"] == "user" else Direction.outbound
        occurred_at = datetime.fromisoformat(entry["at"])
        await ingest_module.ingest(
            business_id=business.id,
            channel=Channel.web,
            direction=direction,
            identity_kind=resolution.identity.kind,
            identity_value=resolution.identity.value,
            external_id=None,
            text=entry["text"],
            occurred_at=occurred_at,
            db=db,
            enqueue_analysis=(direction == Direction.inbound),
        )
    session.transcript = []

    return resolution.customer


async def _do_ingest(
    db: AsyncSession, *, business: Business, session: WebSession,
    customer: Customer, direction: Direction, text: str,
) -> uuid.UUID | None:
    """Returns the new Message's id - book()/queue_booking's own
    provenance rule (source_message_ids required for any non-manual
    intake_channel) needs a real message to cite, not a synthesized one."""
    identities = await resolver.identities_for(customer.id, db)
    if not identities:
        return None
    identity = identities[0]
    result = await ingest_module.ingest(
        business_id=business.id,
        channel=Channel.web,
        direction=direction,
        identity_kind=identity.kind,
        identity_value=identity.value,
        external_id=None,
        text=text,
        occurred_at=_now(),
        db=db,
        enqueue_analysis=(direction == Direction.inbound),
    )
    return result.message.id if result.message else None


async def generate_reply(
    db: AsyncSession, *, business: Business, session: WebSession, text: str,
) -> ReplyOutcome:
    customer = await db.get(Customer, session.customer_id) if session.customer_id else None
    inbound_message_id: uuid.UUID | None = None

    # Record the inbound turn first, so it's part of the context the model
    # actually reads - same ordering voice's _store_turn-then-build uses.
    if customer is not None:
        inbound_message_id = await _do_ingest(db, business=business, session=session, customer=customer, direction=Direction.inbound, text=text)
        context = await agent_context.build(business.id, customer.id, db)
    else:
        _append_transcript(session, role="user", text=text)
        context = await agent_context.build_anonymous(business.id, _transcript_as_recent(session), db)

    events = agent_module.stream_reply(context)
    try:
        first = await events.__anext__()
    except StopAsyncIteration:
        logger.warning("web widget stream_reply produced no events, business=%s", business.id)
        return ReplyOutcome(reply_text="Sorry, something went wrong - please try again.", booked=False, needs_contact=False)

    action = first.action if isinstance(first, agent_module.ReplyStart) else "escalate"

    if action == "no_action":
        async for _ in events:
            pass
        return ReplyOutcome(reply_text="", booked=False, needs_contact=False)

    if action == "escalate":
        gap: str | None = None
        async for ev in events:
            if isinstance(ev, agent_module.ReplyDone):
                gap = ev.gap
        if gap:
            await agent_module.record_gap(business.id, gap, db)
        reply = (
            f"I don't have {gap} on hand right now, but I'll make sure someone follows up with you."
            if gap
            else "I don't have enough to answer that properly, but I'll make sure someone follows up with you."
        )
        if customer is not None:
            await _do_ingest(db, business=business, session=session, customer=customer, direction=Direction.outbound, text=reply)
        else:
            _append_transcript(session, role="assistant", text=reply)
        return ReplyOutcome(reply_text=reply, booked=False, needs_contact=False)

    # A booking attempt with no resolved identity yet: Tier 1 sessions
    # structurally cannot fire book_slot/book_token (both require a real
    # Customer), so this is intercepted before either function is ever
    # called with customer=None - never a fabricated identity, never a
    # false "booked" claim.
    if (first.book_slot or first.book_token) and customer is None:
        async for _ in events:
            pass
        _append_transcript(session, role="assistant", text=_NEEDS_CONTACT_MESSAGE)
        return ReplyOutcome(reply_text=_NEEDS_CONTACT_MESSAGE, booked=False, needs_contact=True)

    # A booking fired synchronously (customer already resolved, so this
    # turn's inbound message was ingested with a real id above) needs that
    # id to cite - book()/try_book_token_from_agent both require
    # source_message_ids for any non-manual intake_channel. If ingest()
    # somehow returned none (a private customer, silently dropped - see
    # ingest.py's own docstring), booking cannot honestly cite anything,
    # so it is treated the same as no identity at all.
    if (first.book_slot or first.book_token) and inbound_message_id is None:
        async for _ in events:
            pass
        reply = "I wasn't able to process that booking - someone will confirm the details with you shortly."
        return ReplyOutcome(reply_text=reply, booked=False, needs_contact=False)

    booked = False
    if first.book_slot and customer is not None:
        appointment = await scheduling_booking.try_book_from_agent(
            db, book_slot=first.book_slot, book_doctor=first.book_doctor,
            book_property=first.book_property, business=business, customer=customer,
            intake_channel=IntakeChannel.web, source_message_ids=[inbound_message_id],
        )
        if appointment is None:
            async for _ in events:
                pass
            reply = "I wasn't able to lock in that exact time - someone will confirm the details with you shortly."
            await _do_ingest(db, business=business, session=session, customer=customer, direction=Direction.outbound, text=reply)
            return ReplyOutcome(reply_text=reply, booked=False, needs_contact=False)
        booked = True
    elif first.book_token and customer is not None:
        entry = await queue_booking.try_book_token_from_agent(
            db, book_token=first.book_token, business=business, customer=customer,
            intake_channel=IntakeChannel.web, source_message_ids=[inbound_message_id],
        )
        if entry is None:
            async for _ in events:
                pass
            reply = "I wasn't able to add you to that queue right now - someone will confirm the details with you shortly."
            await _do_ingest(db, business=business, session=session, customer=customer, direction=Direction.outbound, text=reply)
            return ReplyOutcome(reply_text=reply, booked=False, needs_contact=False)
        booked = True

    chunks: list[str] = []
    async for ev in events:
        if isinstance(ev, agent_module.ReplyChunk):
            chunks.append(ev.text)
    reply_text = " ".join(chunks).strip() or "Got it."

    if customer is not None:
        await _do_ingest(db, business=business, session=session, customer=customer, direction=Direction.outbound, text=reply_text)
    else:
        _append_transcript(session, role="assistant", text=reply_text)

    return ReplyOutcome(reply_text=reply_text, booked=booked, needs_contact=False)
