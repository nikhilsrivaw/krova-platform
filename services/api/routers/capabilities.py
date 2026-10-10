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
from shared.channels.whatsapp import required_templates, template_service
from shared.commands.tools import WRITE_ROLES
from shared.db.models import Business
from shared.utils.logging import get_logger
from shared.verticals.capability_info import SWITCHABLE, CapabilityInfo

logger = get_logger(__name__)

router = APIRouter(prefix="/capabilities", tags=["capabilities"])


class TemplateNeedOut(BaseModel):
    name: str
    purpose: str
    category: str
    # What each {{number}} in the template stands for, in order.
    variables: list[str]
    body: str
    # approved | pending | rejected | missing | other
    status: str
    rejection_reason: str | None
    # KROVA can submit this one for the owner; if not, `note` says why.
    one_click: bool
    note: str | None


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
    # The WhatsApp templates this feature's messages go out as. Without an
    # approved one the feature's sends are silently skipped.
    templates: list[TemplateNeedOut]
    # WhatsApp is connected, so missing templates can be submitted from here.
    can_create_templates: bool


class CapabilityIn(BaseModel):
    enabled: bool


def _out(
    info: CapabilityInfo, business: Business,
    states: dict[str, tuple[str, str | None]], can_create: bool,
) -> CapabilityOut:
    enabled = verticals.has_capability(business, info.key)
    default = info.key in verticals.template_capabilities(business)
    return CapabilityOut(
        key=info.key, label=info.label, description=info.description,
        adds=list(info.adds), setup=info.setup,
        enabled=enabled, default=default, overridden=enabled != default,
        templates=[
            TemplateNeedOut(
                name=req.name, purpose=req.purpose, category=req.category,
                variables=list(req.variables), body=req.body,
                status=states[req.name][0], rejection_reason=states[req.name][1],
                one_click=req.one_click, note=req.note,
            )
            for req in required_templates.for_capability(info.key)
        ],
        can_create_templates=can_create,
    )


async def _can_create(db: DbDep, business_id) -> bool:
    try:
        await template_service.get_connection(db, business_id)
    except template_service.WhatsAppNotReady:
        return False
    return True


async def _business(current_user: CurrentUserDep, db: DbDep) -> Business:
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    return business


@router.get("", response_model=list[CapabilityOut])
async def list_capabilities(current_user: CurrentUserDep, db: DbDep) -> list[CapabilityOut]:
    business = await _business(current_user, db)
    states = await required_templates.statuses(db, business.id)
    can_create = await _can_create(db, business.id)
    return [_out(info, business, states, can_create) for info in SWITCHABLE.values()]


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
    states = await required_templates.statuses(db, business.id)
    return _out(info, business, states, await _can_create(db, business.id))


class CreateTemplatesOut(BaseModel):
    created: list[str]
    already_there: list[str]
    needs_manual: list[str]
    failed: list[dict]


@router.post("/{key}/templates", response_model=CreateTemplatesOut)
async def create_missing_templates(
    key: str, current_user: CurrentUserDep, db: DbDep
) -> CreateTemplatesOut:
    """
    Submit to Meta every template this feature needs that the business does
    not have yet. Safe to press twice: a template that exists in any state is
    left alone. Approval still takes Meta up to a day.
    """
    if current_user.role not in WRITE_ROLES:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the owner or an admin can create message templates."
        )
    if key not in SWITCHABLE:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That feature cannot be switched")

    try:
        report = await required_templates.create_missing(db, current_user.business, key)
    except template_service.WhatsAppNotReady as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await db.commit()
    logger.info(
        "templates for %s: created=%s failed=%s business=%s",
        key, report.created, [f["name"] for f in report.failed], current_user.business,
    )
    return CreateTemplatesOut(
        created=report.created, already_there=report.already_there,
        needs_manual=report.needs_manual, failed=report.failed,
    )
