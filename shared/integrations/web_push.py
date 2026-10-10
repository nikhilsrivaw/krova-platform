"""
Web Push to the app's installed browsers, through VAPID - no third-party
push account needed. Best-effort everywhere: a missing key, a dead endpoint
or a rejected push must never break the request that triggered it.

A subscription the push service reports as gone (404/410) is deleted, so
the table does not grow with installs people have since removed.
"""

import asyncio
import json
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.settings import settings
from shared.db.models import PushSubscription
from shared.utils.logging import get_logger

logger = get_logger(__name__)


def _send_one(sub: dict, payload: dict) -> int | None:
    """Blocking. Returns the HTTP status on failure, None on success."""
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info=sub,
            data=json.dumps(payload),
            vapid_private_key=settings.vapid_private_key,
            vapid_claims={"sub": settings.vapid_subject},
            ttl=3600,
        )
        return None
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning("web push failed status=%s", status)
        return status or 0


async def send_to_users(
    db: AsyncSession, *, business_id: uuid.UUID, user_ids: list[uuid.UUID], payload: dict
) -> int:
    """Push only to these people's installed apps (an assigned chat pings its owner, not everyone)."""
    if not user_ids:
        return 0
    return await send_to_business(db, business_id=business_id, payload=payload, user_ids=user_ids)


async def send_to_business(
    db: AsyncSession, *, business_id: uuid.UUID, payload: dict, user_ids: list[uuid.UUID] | None = None
) -> int:
    """Push to every installed app of this business (or just `user_ids`). Returns how many were sent."""
    if not settings.vapid_private_key:
        return 0

    query = select(PushSubscription).where(PushSubscription.business_id == business_id)
    if user_ids is not None:
        query = query.where(PushSubscription.user_id.in_(user_ids))
    rows = (await db.execute(query)).scalars().all()
    if not rows:
        return 0

    sent = 0
    gone: list[uuid.UUID] = []
    for row in rows:
        sub = {"endpoint": row.endpoint, "keys": {"p256dh": row.p256dh, "auth": row.auth}}
        status = await asyncio.to_thread(_send_one, sub, payload)
        if status is None:
            sent += 1
        elif status in (404, 410):
            gone.append(row.id)

    if gone:
        await db.execute(delete(PushSubscription).where(PushSubscription.id.in_(gone)))
        await db.flush()
    return sent
