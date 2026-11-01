"""
Talk to team page: customers who asked to speak to a person (or are owed a call back), waiting for whoever is free.
The rules live in shared/team/requests.py; the side panel's story is the team briefing (GET /team/briefing/{id}).

Open to everyone on the team - picking these up is the day-to-day work. An agent who only sees their own chats sees
requests from those customers and from customers nobody has yet. Taking a request someone else is on is refused with
their name (owner and admins can take it over); handing one to a named person is for owner and admins.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.audit import activity
from shared.db.models import BusinessMember, Customer, Team, TeamRequest, User
from shared.team import access, conflict
from shared.team import requests as team_requests

router = APIRouter(prefix="/team-requests", tags=["team-requests"])


class PersonOut(BaseModel):
    user_id: str
    name: str | None


class RequestOut(BaseModel):
    id: str
    customer_id: str
    customer_name: str | None
    channel: str
    what: str
    source: str
    status: str
    team: str | None
    taken_by: PersonOut | None
    taken_at: datetime | None
    created_at: datetime
    done_at: datetime | None
    outcome: str | None


class CountOut(BaseModel):
    open: int
    mine: int


class TakeIn(BaseModel):
    take_over: bool = False


class AssignIn(BaseModel):
    user_id: uuid.UUID


class DoneIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)
    cancelled: bool = False


class CreateIn(BaseModel):
    customer_id: uuid.UUID
    what: str = Field(min_length=1, max_length=500)


async def _names(db, user_ids: set) -> dict:
    ids = [u for u in user_ids if u]
    if not ids:
        return {}
    rows = await db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
    return {row[0]: row[1] for row in rows.all()}


async def _out(db, rows: list[tuple]) -> list[RequestOut]:
    names = await _names(db, {r.taken_by_user_id for r, _c, _t in rows})
    return [
        RequestOut(
            id=str(r.id), customer_id=str(r.customer_id), customer_name=customer_name, channel=r.channel, what=r.what,
            source=r.source, status=r.status, team=team_name,
            taken_by=PersonOut(user_id=str(r.taken_by_user_id), name=names.get(r.taken_by_user_id)) if r.taken_by_user_id else None,
            taken_at=r.taken_at, created_at=r.created_at, done_at=r.done_at, outcome=r.outcome,
        )
        for r, customer_name, team_name in rows
    ]


def _base(user):
    return (
        select(TeamRequest, Customer.display_name, Team.name)
        .join(Customer, Customer.id == TeamRequest.customer_id)
        .outerjoin(Team, Team.id == TeamRequest.team_id)
        .where(TeamRequest.business_id == user.business)
    )


async def _visible(db, user, query):
    ids = await access.visible_ids(db, user)
    return query if ids is None else query.where(TeamRequest.customer_id.in_(ids))


async def _request(db, user, request_id: uuid.UUID) -> TeamRequest:
    request = await db.get(TeamRequest, request_id)
    if request is None or request.business_id != user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    await access.check_id(db, user, request.customer_id)
    return request


async def _one(db, user, request: TeamRequest) -> RequestOut:
    rows = (await db.execute(_base(user).where(TeamRequest.id == request.id))).all()
    return (await _out(db, rows))[0]


@router.get("", response_model=list[RequestOut])
async def list_requests(
    current_user: CurrentUserDep, db: DbDep,
    state: str = Query(default="waiting", pattern="^(waiting|open|taken|done|all)$"),
    mine: bool = False, limit: int = Query(default=100, ge=1, le=300),
) -> list[RequestOut]:
    """waiting = open + taken (the work), oldest first; done/all newest first."""
    query = await _visible(db, current_user, _base(current_user))
    if state == "waiting":
        query = query.where(TeamRequest.status.in_(team_requests.OPEN)).order_by(TeamRequest.created_at.asc())
    elif state == "all":
        query = query.order_by(TeamRequest.created_at.desc())
    elif state == "done":
        query = query.where(TeamRequest.status.in_(("done", "cancelled"))).order_by(TeamRequest.done_at.desc().nullslast())
    else:
        query = query.where(TeamRequest.status == state).order_by(TeamRequest.created_at.asc())
    if mine:
        query = query.where(TeamRequest.taken_by_user_id == current_user.id)
    return await _out(db, (await db.execute(query.limit(limit))).all())


@router.get("/count", response_model=CountOut)
async def count_requests(current_user: CurrentUserDep, db: DbDep) -> CountOut:
    """For the menu badge: nobody has taken these yet; and how many the signed-in person is on."""
    base = await _visible(db, current_user, select(func.count(TeamRequest.id)).where(TeamRequest.business_id == current_user.business))
    waiting = (await db.execute(base.where(TeamRequest.status == "open"))).scalar() or 0
    mine = (await db.execute(base.where(TeamRequest.status == "taken", TeamRequest.taken_by_user_id == current_user.id))).scalar() or 0
    return CountOut(open=int(waiting), mine=int(mine))


@router.post("", response_model=RequestOut, status_code=status.HTTP_201_CREATED)
async def add_request(body: CreateIn, current_user: CurrentUserDep, db: DbDep) -> RequestOut:
    """Added by hand - e.g. a customer asked in person or on someone's own phone."""
    customer = await access.check_id(db, current_user, body.customer_id)
    if customer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")
    request = await team_requests.open_request(
        db, business_id=current_user.business, customer=customer, channel="whatsapp", what=body.what, source="staff",
    )
    return await _one(db, current_user, request)


@router.post("/{request_id}/take", response_model=RequestOut)
async def take_request(request_id: uuid.UUID, body: TakeIn, current_user: CurrentUserDep, db: DbDep) -> RequestOut:
    """'I'll take it' - the request and the customer's chat become yours."""
    request = await _request(db, current_user, request_id)
    if request.status not in team_requests.OPEN:
        raise HTTPException(status.HTTP_409_CONFLICT, "This request is already closed.")
    try:
        await team_requests.take(db, request=request, user_id=current_user.id,
                                 override=body.take_over and access.supervisor(current_user))
    except team_requests.Taken as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{exc.name} is already on this.") from exc
    activity.note(customer=str(request.customer_id))
    return await _one(db, current_user, request)


@router.post("/{request_id}/release", response_model=RequestOut)
async def release_request(request_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> RequestOut:
    request = await _request(db, current_user, request_id)
    if request.taken_by_user_id not in (None, current_user.id) and not access.supervisor(current_user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the person on it, or the owner or an admin, can put it back.")
    await team_requests.release(db, request=request)
    return await _one(db, current_user, request)


@router.post("/{request_id}/assign", response_model=RequestOut)
async def assign_request(request_id: uuid.UUID, body: AssignIn, current_user: CurrentUserDep, db: DbDep) -> RequestOut:
    """Owner and admins hand a request to a named person, who is told."""
    if not access.supervisor(current_user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner or an admin can hand a request to someone.")
    request = await _request(db, current_user, request_id)
    member = (await db.execute(select(BusinessMember).where(
        BusinessMember.business_id == current_user.business, BusinessMember.user_id == body.user_id))).scalar_one_or_none()
    if member is None:
        raise HTTPException(422, "That person is not on your team.")
    await team_requests.take(db, request=request, user_id=body.user_id, override=True)
    customer = await db.get(Customer, request.customer_id)
    await conflict.notify(
        db, business_id=current_user.business, user_id=body.user_id, kind="team_request",
        title=f"Talk to team: {(customer.display_name if customer else None) or 'a customer'}",
        body=f"Given to you - {request.what}", url="/app/talk-to-team",
    )
    return await _one(db, current_user, request)


@router.post("/{request_id}/done", response_model=RequestOut)
async def finish_request(request_id: uuid.UUID, body: DoneIn, current_user: CurrentUserDep, db: DbDep) -> RequestOut:
    """Close it with what happened - the note stays on the customer for the whole team."""
    request = await _request(db, current_user, request_id)
    if request.status not in team_requests.OPEN:
        raise HTTPException(status.HTTP_409_CONFLICT, "This request is already closed.")
    if request.taken_by_user_id not in (None, current_user.id) and not access.supervisor(current_user):
        raise HTTPException(status.HTTP_409_CONFLICT, "Someone else is on this - only they, or the owner or an admin, can close it.")
    await team_requests.finish(db, request=request, user_id=current_user.id, note=body.note, cancelled=body.cancelled)
    return await _one(db, current_user, request)
