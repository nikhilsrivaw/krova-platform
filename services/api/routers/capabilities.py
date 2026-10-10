"""
Which optional modules a business has switched on - the "Features" list.

A vertical (market type) decides what a business starts with; this is where
the business decides what it actually uses. Reading is open to anyone on the
team, because the sidebar and every gated page already depend on the result.
Changing it is the owner's or an admin's call: it adds and removes whole
pages and background behaviour.
"""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from services.api.dependencies import CurrentUserDep, DbDep
from shared import verticals
from shared.commands.tools import WRITE_ROLES
from shared.db.models import Business
from shared.utils.logging import get_logger
from shared.verticals.capability_info import SWITCHABLE, CapabilityInfo

logger = get_logger(__name__)

router = APIRouter(prefix="/capabilities", tags=["capabilities"])


class CapabilityOut(BaseModel):
    key: str
    label: str
    description: str
    adds: list[str]
    setup: str | None
    # What the business has right now.
    enabled: bool
    # What its market type would give it with no changes of its own.
    default: bool
    # enabled differs from default - the business changed this one itself.
    overridden: bool


class CapabilityIn(BaseModel):
    enabled: bool


def _out(info: CapabilityInfo, business: Business) -> CapabilityOut:
    enabled = verticals.has_capability(business, info.key)
    default = info.key in verticals.template_capabilities(business)
    return CapabilityOut(
        key=info.key, label=info.label, description=info.description,
        adds=list(info.adds), setup=info.setup,
        enabled=enabled, default=default, overridden=enabled != default,
    )


async def _business(current_user: CurrentUserDep, db: DbDep) -> Business:
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    return business


@router.get("", response_model=list[CapabilityOut])
async def list_capabilities(current_user: CurrentUserDep, db: DbDep) -> list[CapabilityOut]:
    business = await _business(current_user, db)
    return [_out(info, business) for info in SWITCHABLE.values()]


@router.put("/{key}", response_model=CapabilityOut)
async def set_capability(
    key: str, body: CapabilityIn, current_user: CurrentUserDep, db: DbDep
) -> CapabilityOut:
    if current_user.role not in WRITE_ROLES:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the owner or an admin can change which features are on."
        )
    info = SWITCHABLE.get(key)
    if info is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That feature cannot be switched")

    business = await _business(current_user, db)
    verticals.set_capability(business, key, body.enabled)
    await db.commit()
    logger.info(
        "capability %s %s for business=%s by user=%s",
        key, "on" if body.enabled else "off", business.id, current_user.id,
    )
    return _out(info, business)
