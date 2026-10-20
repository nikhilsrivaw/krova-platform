"""
The team: who is on this business, and the owner's tools for adding people.

GET /team is the list every assignment dropdown needs - open to everyone on the
team. Everything that adds, changes or removes a person is owner/admin only,
and the finer rules (an admin may only manage agents, nobody touches the owner)
live in shared/team/members.py so they hold however it is reached.

A teammate is created with a Team ID and a password. The password is returned
exactly once, in the response that creates it (or resets it); only its hash is
stored, so it cannot be shown again - the owner resets it if it is lost.
"""

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from services.api.dependencies import CurrentUserDep, DbDep, OwnerOrAdminDep
from shared.audit import activity
from shared.db.models import Business, BusinessMember, Case, CaseStatus, DraftStatus, Customer, Escalation, MessageDraft, User
from shared.identity.normalise import InvalidIdentifier, normalise_phone
from shared.team import access, briefing, human_calls, members
from shared.team import settings as team_settings

router = APIRouter(prefix="/team", tags=["team"])


class TeamMemberOut(BaseModel):
    user_id: str
    full_name: str | None
    email: str | None
    role: str
    # Owner/admin only - what the person types to sign in, and their state.
    team_id: str | None = None
    last_login_at: datetime | None = None
    must_change_password: bool | None = None
    locked: bool | None = None
    available: bool = True
    # Where calls for this person ring. Shown to owner/admin, and to the person themselves.
    phone: str | None = None


@router.get("", response_model=list[TeamMemberOut])
async def list_team(current_user: CurrentUserDep, db: DbDep) -> list[TeamMemberOut]:
    supervisor = current_user.role in ("owner", "admin")
    rows = await db.execute(
        select(BusinessMember, User)
        .join(User, User.id == BusinessMember.user_id)
        .where(BusinessMember.business_id == current_user.business, User.is_active == True)  # noqa: E712
        .order_by(User.full_name, User.email)
    )
    now = datetime.now(timezone.utc)
    out = []
    for member, user in rows.all():
        item = TeamMemberOut(
            user_id=str(member.user_id),
            full_name=user.full_name,
            email=user.email,
            role=members.role_value(member.role),
            available=bool(member.available),
        )
        if supervisor or member.user_id == current_user.id:
            item.phone = member.phone
        if supervisor:
            item.team_id = user.username
            item.last_login_at = user.last_login_at
            item.must_change_password = bool(user.must_change_password)
            item.locked = bool(user.locked_until and user.locked_until > now)
        out.append(item)
    return out


class NewMemberIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    # The part before the '@' in the Team ID, e.g. 'rahul'.
    handle: str = Field(min_length=2, max_length=30)
    role: Literal["admin", "agent"] = "agent"
    # Optional: leave empty and one is generated.
    password: str | None = Field(default=None, max_length=256)


class Credentials(BaseModel):
    user_id: str
    full_name: str | None
    role: str
    team_id: str
    # Shown once. Only its hash is kept.
    password: str


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _http(exc: members.TeamError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@router.post("/members", response_model=Credentials, status_code=201)
async def add_member(
    body: NewMemberIn, response: Response, current_user: OwnerOrAdminDep, db: DbDep
) -> Credentials:
    _no_store(response)
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(404, "Business not found")
    try:
        user, team_id, password = await members.create_member(
            db, business=business, actor_id=current_user.id, actor_role=current_user.role,
            full_name=body.full_name, handle=body.handle, role=body.role, password=body.password,
        )
    except members.TeamError as exc:
        raise _http(exc) from exc
    except ValueError as exc:  # password too short / long
        raise HTTPException(422, str(exc)) from exc
    activity.note(member=team_id, role=body.role)
    return Credentials(
        user_id=str(user.id), full_name=user.full_name, role=body.role,
        team_id=team_id, password=password,
    )


class RoleIn(BaseModel):
    role: Literal["admin", "agent"]


@router.patch("/members/{user_id}")
async def set_role(user_id: uuid.UUID, body: RoleIn, current_user: OwnerOrAdminDep, db: DbDep) -> dict:
    try:
        await members.change_role(
            db, business_id=current_user.business, actor_id=current_user.id,
            actor_role=current_user.role, user_id=user_id, role=body.role,
        )
    except members.TeamError as exc:
        raise _http(exc) from exc
    activity.note(member=str(user_id), role=body.role)
    return {"user_id": str(user_id), "role": body.role}


class ResetIn(BaseModel):
    password: str | None = Field(default=None, max_length=256)


@router.post("/members/{user_id}/reset-password", response_model=Credentials)
async def reset_member_password(
    user_id: uuid.UUID, body: ResetIn, response: Response, current_user: OwnerOrAdminDep, db: DbDep
) -> Credentials:
    _no_store(response)
    try:
        user, password = await members.reset_password(
            db, business_id=current_user.business, actor_id=current_user.id,
            actor_role=current_user.role, user_id=user_id, password=body.password,
        )
    except members.TeamError as exc:
        raise _http(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    role = (
        await db.execute(
            select(BusinessMember.role).where(
                BusinessMember.business_id == current_user.business, BusinessMember.user_id == user_id
            )
        )
    ).scalar_one()
    activity.note(member=str(user_id))
    return Credentials(
        user_id=str(user.id), full_name=user.full_name, role=members.role_value(role),
        team_id=user.username or "", password=password,
    )


@router.delete("/members/{user_id}")
async def remove_member(user_id: uuid.UUID, current_user: OwnerOrAdminDep, db: DbDep) -> dict:
    try:
        released = await members.remove_member(
            db, business_id=current_user.business, actor_id=current_user.id,
            actor_role=current_user.role, user_id=user_id,
        )
    except members.TeamError as exc:
        raise _http(exc) from exc
    activity.note(member=str(user_id), released=released)
    return {"removed": str(user_id), "released": released}


class TeamSettings(BaseModel):
    auto_assign_on_reply: bool = True
    agent_visibility: Literal["all", "assigned"] = "all"
    routing: Literal["manual", "round_robin"] = "manual"
    sla_minutes: int | None = Field(default=None, ge=1, le=1440)


@router.get("/settings", response_model=TeamSettings)
async def get_team_settings(current_user: CurrentUserDep, db: DbDep) -> TeamSettings:
    business = await db.get(Business, current_user.business)
    return TeamSettings(**team_settings.read(business))


@router.put("/settings", response_model=TeamSettings)
async def put_team_settings(body: TeamSettings, current_user: OwnerOrAdminDep, db: DbDep) -> TeamSettings:
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(404, "Business not found")
    value = team_settings.write(
        business, auto_assign_on_reply=body.auto_assign_on_reply, agent_visibility=body.agent_visibility,
        routing=body.routing, sla_minutes=body.sla_minutes,
    )
    activity.note(**{k: str(v).lower() for k, v in value.items()})
    return TeamSettings(**value)


class MyWork(BaseModel):
    chats: int
    drafts_waiting: int
    escalations: int
    cases: int
    # Calls I spoke on and have not written up (the promises made on them are not in the ledger yet).
    calls_to_write_up: int = 0


@router.get("/my-work", response_model=MyWork)
async def my_work(current_user: CurrentUserDep, db: DbDep) -> MyWork:
    """What is mine right now: chats I own, AI replies waiting on those chats, escalations I hold, open cases."""
    biz, me = current_user.business, current_user.id

    async def count(stmt) -> int:
        return int((await db.execute(stmt)).scalar_one())

    chats = await count(select(func.count(Customer.id)).where(Customer.business_id == biz, Customer.assigned_to_user_id == me))
    drafts = await count(
        select(func.count(MessageDraft.id))
        .join(Customer, Customer.id == MessageDraft.customer_id)
        .where(MessageDraft.business_id == biz, MessageDraft.status == DraftStatus.pending, Customer.assigned_to_user_id == me)
    )
    escalations = await count(
        select(func.count(Escalation.id)).where(
            Escalation.business_id == biz, Escalation.assigned_to_user_id == me,
            Escalation.status.in_(("open", "in_progress")),
        )
    )
    cases = await count(
        select(func.count(Case.id)).where(
            Case.business_id == biz, Case.assigned_to_user_id == me, Case.status != CaseStatus.closed,
        )
    )
    calls = len(await human_calls.pending_wrapups(db, business_id=biz, user_id=me))
    return MyWork(chats=chats, drafts_waiting=drafts, escalations=escalations, cases=cases, calls_to_write_up=calls)


@router.get("/briefing/{customer_id}")
async def customer_briefing(customer_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> dict:
    """Who this customer is and what is open with them - shown before a call, a claim or a reply."""
    supervisor = current_user.role in ("owner", "admin")
    await access.check_id(db, current_user, customer_id)
    found = await briefing.build(
        db, business_id=current_user.business, customer_id=customer_id, private_ok=supervisor,
    )
    if found is None:
        raise HTTPException(404, "Customer not found")
    return found.to_dict()


class PhoneIn(BaseModel):
    phone: str | None = None  # "" or null clears it


def _clean_phone(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    try:
        return normalise_phone(raw)
    except InvalidIdentifier as exc:
        raise HTTPException(422, f"phone: {exc}") from exc


async def _member(db, business_id: uuid.UUID, user_id: uuid.UUID) -> BusinessMember:
    member = (await db.execute(
        select(BusinessMember).where(
            BusinessMember.business_id == business_id, BusinessMember.user_id == user_id)
    )).scalar_one_or_none()
    if member is None:
        raise HTTPException(404, "Not on this team")
    return member


@router.put("/me/phone")
async def set_my_phone(body: PhoneIn, current_user: CurrentUserDep, db: DbDep) -> dict:
    """The number calls for me ring. Empty = I take no calls."""
    member = await _member(db, current_user.business, current_user.id)
    member.phone = _clean_phone(body.phone)
    activity.note(phone_set=str(member.phone is not None).lower())
    return {"phone": member.phone}


@router.patch("/members/{user_id}/phone")
async def set_member_phone(user_id: uuid.UUID, body: PhoneIn, current_user: OwnerOrAdminDep, db: DbDep) -> dict:
    member = await _member(db, current_user.business, user_id)
    member.phone = _clean_phone(body.phone)
    activity.note(phone_set=str(member.phone is not None).lower(), for_user=str(user_id))
    return {"phone": member.phone}


class AvailabilityIn(BaseModel):
    available: bool


@router.put("/me/availability")
async def set_my_availability(body: AvailabilityIn, current_user: CurrentUserDep, db: DbDep) -> dict:
    """Away = no new chats routed to me. My existing chats stay mine."""
    member = (await db.execute(
        select(BusinessMember).where(
            BusinessMember.business_id == current_user.business, BusinessMember.user_id == current_user.id)
    )).scalar_one_or_none()
    if member is None:
        raise HTTPException(404, "Not on this team")
    member.available = body.available
    activity.note(available=str(body.available).lower())
    return {"available": member.available}


class TransferIn(BaseModel):
    user_id: uuid.UUID
    password: str = Field(min_length=1, max_length=256)


@router.post("/transfer-ownership")
async def transfer_ownership(body: TransferIn, current_user: OwnerOrAdminDep, db: DbDep) -> dict:
    try:
        await members.transfer_ownership(
            db, business_id=current_user.business, owner_id=current_user.id,
            actor_role=current_user.role, password=body.password, to_user_id=body.user_id,
        )
    except members.TeamError as exc:
        raise _http(exc) from exc
    activity.note(new_owner=str(body.user_id))
    return {"new_owner": str(body.user_id), "you_are_now": "admin"}
