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
from shared.db.models import PushSubscription

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
