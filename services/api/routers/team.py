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
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep, OwnerOrAdminDep
from shared.audit import activity
from shared.db.models import Business, BusinessMember, User
from shared.team import members

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
        )
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
