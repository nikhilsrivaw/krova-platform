"""
Settings -> "What the AI can do for customers": which changes the AI may make by itself, on which channel, and how
close to a booking it may still change it. The rules themselves live in shared/ai/actions.py.

Owner and admins only: this decides what the AI does to customers' bookings and orders without a person.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.api.dependencies import DbDep, OwnerOrAdminDep
from shared import verticals
from shared.ai import actions as rules
from shared.db.models import Business

router = APIRouter(prefix="/ai-actions", tags=["ai-actions"])

# What each action needs this business to have switched on (Settings -> Features).


class ActionOut(BaseModel):
    key: str
    label: str
    channels: list[str]
    # False when the business has not switched on what this needs (bookings, the walk-in queue, orders).
    available: bool
    asks_to_confirm: bool


class SettingsOut(BaseModel):
    actions: list[ActionOut]
    cutoff_hours: int | None
    channels: list[str]


class SettingsIn(BaseModel):
    actions: dict[str, list[str]] = Field(default_factory=dict)
    cutoff_hours: int | None = Field(default=None, ge=1, le=rules.MAX_CUTOFF_HOURS)


def _out(business: Business) -> SettingsOut:
    current = rules.read(business)
    return SettingsOut(
        actions=[
            ActionOut(key=key, label=label, channels=current["actions"][key],
                      available=rules.available(business, key), asks_to_confirm=key in rules.CHANGES)
            for key, (label, _default) in rules.ACTIONS.items()
        ],
        cutoff_hours=current["cutoff_hours"], channels=list(rules.CHANNELS),
    )


@router.get("", response_model=SettingsOut)
async def get_settings(current_user: OwnerOrAdminDep, db: DbDep) -> SettingsOut:
    return _out(await db.get(Business, current_user.business))


@router.put("", response_model=SettingsOut)
async def put_settings(body: SettingsIn, current_user: OwnerOrAdminDep, db: DbDep) -> SettingsOut:
    business = await db.get(Business, current_user.business)
    unknown = set(body.actions) - set(rules.ACTIONS)
    if unknown:
        raise HTTPException(422, f"Unknown action: {', '.join(sorted(unknown))}")
    merged = {**rules.read(business)["actions"], **body.actions}
    rules.write(business, actions=merged, cutoff_hours=body.cutoff_hours)
    await db.flush()
    return _out(business)
