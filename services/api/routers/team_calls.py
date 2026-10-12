"""
Calls a team member makes or takes: start one, watch it, write it up.

Open to everyone on the team, not only owner/admin - this is what staff do all day. The same
rules as replying apply (a teammate's customer is refused with their name), and a paid-up
wallet and plan are required, because a call costs real money.
"""

import uuid
from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.api.dependencies import CurrentUserDep, DbDep
from shared.audit import activity
from shared.db.models import Call, Customer, Direction, User
from shared.team import human_calls

router = APIRouter(prefix="/calls", tags=["calls"])


class CallOut(BaseModel):
    id: str
    customer_id: str | None
    customer_name: str | None
    # ringing_you -> talking -> ended
    state: str
    direction: str  # placed | received
    started_at: datetime
    answered_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    # Placed calls only: did the customer pick up?
    reached_customer: bool | None
    wrapped_up: bool


async def _out(db, call: Call) -> CallOut:
    customer = await db.get(Customer, call.customer_id) if call.customer_id else None
    placed = call.direction == Direction.outbound
    return CallOut(
        id=str(call.id),
        customer_id=str(call.customer_id) if call.customer_id else None,
        customer_name=customer.display_name if customer else None,
        state=human_calls.state_of(call),
        direction="placed" if placed else "received",
        started_at=call.started_at, answered_at=call.answered_at, ended_at=call.ended_at,
        duration_seconds=call.duration_seconds,
        reached_customer=(call.hangup_cause == "completed") if placed and call.ended_at else None,
        wrapped_up=call.wrapped_up_at is not None,
    )


class StartIn(BaseModel):
    customer_id: uuid.UUID


@router.post("/start", response_model=CallOut)
async def start_call(body: StartIn, current_user: CurrentUserDep, db: DbDep) -> CallOut:
    """Ring my phone; when I pick up the customer is dialled from the business number."""
    call = await human_calls.start(
        db, business_id=current_user.business, user_id=current_user.id,
        role=current_user.role, customer_id=body.customer_id,
    )
    return await _out(db, call)


@router.get("/pending/wrapup", response_model=list[CallOut])
async def calls_to_write_up(current_user: CurrentUserDep, db: DbDep) -> list[CallOut]:
    """Calls I spoke on and have not written up yet."""
    rows = await human_calls.pending_wrapups(db, business_id=current_user.business, user_id=current_user.id)
    return [await _out(db, c) for c in rows]


async def _mine(db, call_id: uuid.UUID, current_user) -> Call:
    call = await db.get(Call, call_id)
    if call is None or call.business_id != current_user.business:
        raise HTTPException(404, "Call not found")
    if call.handled_by_user_id != current_user.id and current_user.role not in ("owner", "admin"):
        raise HTTPException(403, "This call was with a teammate")
    return call


@router.get("/{call_id}", response_model=CallOut)
async def get_call(call_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> CallOut:
    return await _out(db, await _mine(db, call_id, current_user))


class PromiseIn(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    due_on: date | None = None
    kind: str | None = None  # payment | delivery | callback | document | meeting | other


class WrapUpIn(BaseModel):
    note: str = Field(default="", max_length=2000)
    promises: list[PromiseIn] = []


@router.post("/{call_id}/wrapup")
async def wrap_up_call(call_id: uuid.UUID, body: WrapUpIn, current_user: CurrentUserDep, db: DbDep) -> dict:
    """Write up a call: one line for the team, and any promises made - those go straight to the ledger."""
    call = await _mine(db, call_id, current_user)
    user = await db.get(User, current_user.id)
    filed = await human_calls.wrap_up(
        db, call=call, user=user, note=body.note,
        promises=[p.model_dump() for p in body.promises],
    )
    activity.note(promises_filed=str(filed))
    return {"ok": True, "promises_filed": filed}
