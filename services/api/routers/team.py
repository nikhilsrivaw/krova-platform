"""
Who's on this business's team - the list every assignment dropdown needs.

Without this, "assign to a teammate" has nobody to assign to: Customer and
Case both already carry assigned_to_user_id, but neither the conversations
inbox nor the cases screen had any way to learn who a business's own team
actually is.
"""

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import BusinessMember, User

router = APIRouter(prefix="/team", tags=["team"])


class TeamMemberOut(BaseModel):
    user_id: str
    full_name: str | None
    email: str
    role: str


@router.get("", response_model=list[TeamMemberOut])
async def list_team(current_user: CurrentUserDep, db: DbDep) -> list[TeamMemberOut]:
    rows = await db.execute(
        select(BusinessMember, User)
        .join(User, User.id == BusinessMember.user_id)
        .where(BusinessMember.business_id == current_user.business, User.is_active == True)  # noqa: E712
        .order_by(User.full_name, User.email)
    )
    return [
        TeamMemberOut(
            user_id=str(member.user_id),
            full_name=user.full_name,
            email=user.email,
            role=member.role.value if hasattr(member.role, "value") else str(member.role),
        )
        for member, user in rows.all()
    ]
