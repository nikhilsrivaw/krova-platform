"""
Web Push subscriptions for the KROVA app. The browser asks for the VAPID
public key, subscribes, and posts the subscription here. Re-subscribing the
same install (same endpoint) updates its row instead of adding a second one.
"""

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.config.settings import settings
from shared.db.models import BusinessMember, PushSubscription
from shared.integrations.web_push import PUSH_KINDS

router = APIRouter(prefix="/push", tags=["push"])


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class SubscribeIn(BaseModel):
    endpoint: str
    keys: PushKeys


@router.get("/vapid-public-key")
async def vapid_public_key(current_user: CurrentUserDep) -> dict:
    if not settings.vapid_public_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Notifications are not configured yet.")
    return {"public_key": settings.vapid_public_key}


@router.post("/subscribe", status_code=status.HTTP_204_NO_CONTENT)
async def subscribe(body: SubscribeIn, current_user: CurrentUserDep, db: DbDep) -> None:
    if not body.endpoint.startswith("https://"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Push endpoint must be https.")
    existing = (await db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
    )).scalar_one_or_none()
    if existing is None:
        db.add(PushSubscription(
            business_id=current_user.business, user_id=current_user.id,
            endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth,
        ))
    else:
        existing.business_id = current_user.business
        existing.user_id = current_user.id
        existing.p256dh = body.keys.p256dh
        existing.auth = body.keys.auth
    await db.commit()


@router.delete("/subscribe", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe(current_user: CurrentUserDep, db: DbDep, endpoint: str = Query(...)) -> None:
    await db.execute(delete(PushSubscription).where(
        PushSubscription.endpoint == endpoint,
        PushSubscription.user_id == current_user.id,
    ))
    await db.commit()


# ── which kinds of notification this person wants ─────────────────────────────

class PushKindOut(BaseModel):
    key: str
    label: str
    on: bool


class PushPreferencesIn(BaseModel):
    # The kinds to switch off; anything not listed is on.
    muted: list[str]


async def _membership(current_user, db) -> BusinessMember:
    member = (await db.execute(
        select(BusinessMember).where(
            BusinessMember.business_id == current_user.business, BusinessMember.user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You are not on this team")
    return member


def _kinds(member: BusinessMember) -> list[PushKindOut]:
    muted = set(member.push_muted or [])
    return [PushKindOut(key=k, label=label, on=k not in muted) for k, label in PUSH_KINDS.items()]


@router.get("/preferences", response_model=list[PushKindOut])
async def push_preferences(current_user: CurrentUserDep, db: DbDep) -> list[PushKindOut]:
    """Each kind of phone notification, and whether this person gets it. Their own choice, per business."""
    return _kinds(await _membership(current_user, db))


@router.put("/preferences", response_model=list[PushKindOut])
async def set_push_preferences(body: PushPreferencesIn, current_user: CurrentUserDep, db: DbDep) -> list[PushKindOut]:
    unknown = [k for k in body.muted if k not in PUSH_KINDS]
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown kind: {unknown[0]}")
    member = await _membership(current_user, db)
    member.push_muted = sorted(set(body.muted))
    await db.commit()
    return _kinds(member)
