"""
A business's own systems calling Krova directly - not the embeddable
widget (that's for a browser; this is server-to-server), and not the
dashboard (that's a person; this is authenticated by ApiKey, see
services/api/dependencies.py::get_api_key_business).

Deliberately stateless, unlike the widget's session-carrying design: a
business's own backend already knows what it wants to ask or book in one
call, it doesn't need Krova to hold conversation state between requests
the way a chat UI does. Every endpoint here reuses an existing function
another surface already depends on - the point is that this API can never
give a different answer than the widget, the kiosk, or the agent would,
because it is the same functions underneath, not a parallel implementation.
"""

import uuid
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from services.api.dependencies import ApiKeyBusinessDep, DbDep
from shared.ai import agent as agent_module
from shared.ai import context as agent_context
from shared.db.models import Doctor, IdentityKind, IntakeChannel, Shift
from shared.identity import resolver as identity_resolver
from shared.scheduling import availability, booking as scheduling_booking, queue_booking
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/public-api", tags=["public-api"])


# ── Ask ──────────────────────────────────────────────────────────────────

class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class AskOut(BaseModel):
    answer: str
    escalated: bool


@router.post("/ask", response_model=AskOut)
async def ask(body: AskIn, business: ApiKeyBusinessDep, db: DbDep) -> AskOut:
    # A one-shot question, not a conversation - the model reads only this
    # business's own real data (same build_anonymous the widget's Tier 1
    # uses) plus the single question, nothing carried between calls. The
    # "recent" shape matches AgentContext.render()'s own expectation
    # exactly (direction/channel/text), not an arbitrary dict.
    context = await agent_context.build_anonymous(
        business.id,
        [{"direction": "inbound", "channel": "api", "text": body.question}],
        db,
    )

    events = agent_module.stream_reply(context)
    try:
        first = await events.__anext__()
    except StopAsyncIteration:
        return AskOut(answer="Sorry, something went wrong - please try again.", escalated=True)

    action = first.action if isinstance(first, agent_module.ReplyStart) else "escalate"

    if action == "escalate":
        gap: str | None = None
        async for ev in events:
            if isinstance(ev, agent_module.ReplyDone):
                gap = ev.gap
        await agent_module.notify_escalation(
            business.id, reason=gap or "needs review", customer_id=None, channel="api", db=db,
        )
        if gap:
            await agent_module.record_gap(business.id, gap, db)
        answer = (
            f"I don't have {gap} on hand right now - someone will follow up."
            if gap else "I don't have enough to answer that properly - someone will follow up."
        )
        return AskOut(answer=answer, escalated=True)

    chunks: list[str] = []
    async for ev in events:
        if isinstance(ev, agent_module.ReplyChunk):
            chunks.append(ev.text)
    return AskOut(answer=" ".join(chunks).strip() or "Got it.", escalated=False)


# ── Availability ─────────────────────────────────────────────────────────

class SlotOut(BaseModel):
    starts_at: datetime
    ends_at: datetime


class AvailabilityOut(BaseModel):
    slots: list[SlotOut] = []
    open_shifts: list[str] = []


@router.get("/availability", response_model=AvailabilityOut)
async def get_availability(
    business: ApiKeyBusinessDep, db: DbDep,
    doctor_id: uuid.UUID | None = Query(default=None),
    on_date: date | None = Query(default=None, alias="date"),
) -> AvailabilityOut:
    if doctor_id is not None:
        doctor = await db.get(Doctor, doctor_id)
        if doctor is None or doctor.business_id != business.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Doctor not found")
        slots = await availability.open_slots(db, business=business, doctor=doctor, on_date=on_date or date.today())
        return AvailabilityOut(slots=[SlotOut(starts_at=s.starts_at, ends_at=s.ends_at) for s in slots])

    summary = await queue_booking.open_shift_summary(db, business_id=business.id)
    return AvailabilityOut(open_shifts=[shift.value for shift, _count in summary])


# ── Bookings ─────────────────────────────────────────────────────────────

class BookingCustomerIn(BaseModel):
    name: str | None = None
    phone: str = Field(min_length=6, max_length=20)


class BookingIn(BaseModel):
    customer: BookingCustomerIn
    doctor_id: uuid.UUID | None = None
    starts_at: datetime | None = None
    shift: Shift | None = None


class BookingOut(BaseModel):
    booked: bool
    appointment_id: str | None = None
    queue_entry_id: str | None = None
    queue_number: int | None = None


@router.post("/bookings", response_model=BookingOut, status_code=status.HTTP_201_CREATED)
async def create_booking(body: BookingIn, business: ApiKeyBusinessDep, db: DbDep) -> BookingOut:
    if not body.doctor_id and not body.shift:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide either doctor_id+starts_at or shift")

    resolution = await identity_resolver.resolve(
        business.id, IdentityKind.phone, body.customer.phone, db, display_name=body.customer.name,
    )

    # No conversation to cite - same as a kiosk or staff check-in, and the
    # exact reason book()/issue_token() already exempt IntakeChannel.manual
    # from requiring source_message_ids.
    if body.doctor_id and body.starts_at:
        doctor = await db.get(Doctor, body.doctor_id)
        if doctor is None or doctor.business_id != business.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Doctor not found")
        same_day = await availability.open_slots(db, business=business, doctor=doctor, on_date=body.starts_at.date())
        slot = next((s for s in same_day if s.starts_at == body.starts_at), None)
        if slot is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "That slot is no longer available")
        try:
            appointment = await scheduling_booking.book(
                db, business_id=business.id, doctor=doctor, customer=resolution.customer,
                slot=slot, intake_channel=IntakeChannel.manual,
            )
        except scheduling_booking.SlotUnavailable as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return BookingOut(booked=True, appointment_id=str(appointment.id))

    try:
        entry = await queue_booking.issue_token(
            db, business_id=business.id, shift=body.shift, customer_id=resolution.customer.id,
            doctor_id=None, intake_channel=IntakeChannel.manual,
        )
    except queue_booking.ShiftNotOpen as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return BookingOut(booked=True, queue_entry_id=str(entry.id), queue_number=entry.queue_number)
