"""
Outbound webhook delivery - the Zapier-shaped "send booking events wherever
a business already points a catch-hook" integration.

Signing mirrors shared/channels/whatsapp/signature.py's own inbound
verification, in reverse: HMAC-SHA256 of the raw JSON body, keyed on a
secret generated for this one endpoint, in an X-Krova-Signature-256
header. Same reasoning as that module - the URL a business gives us is not
a secret, the signature is what proves the payload came from Krova and
was not altered or forged in transit.

Delivery goes through the existing Postgres job queue (shared/db/queue.py)
rather than an inline call at booking time - the queue already has
retry/backoff/max_attempts built in, and a business's endpoint being slow
or down must never block the booking that triggered it.
"""

import hashlib
import hmac
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db import queue
from shared.db.models import OutboundWebhook
from shared.utils.logging import get_logger

logger = get_logger(__name__)

QUEUE = "outbound_webhook"


def sign(secret: str, raw_body: bytes) -> str:
    """HMAC-SHA256 of the raw body, hex-encoded - what the receiving side
    recomputes and compares (constant-time) to confirm this came from us."""
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


async def dispatch_event(
    db: AsyncSession, *, business_id: uuid.UUID, event_type: str, payload: dict
) -> int:
    """
    Enqueue a delivery for every active webhook this business has
    subscribed to `event_type`.

    Does not commit - same contract as queue.enqueue itself: the caller's
    own transaction (the booking that just succeeded) owns the commit, so
    the job becomes visible exactly when the booking does, never before.
    Never raises - a malformed or unreachable target must not undo a real
    booking; wrap this call at each call site the same way the existing
    notify.send_* calls already are.
    """
    result = await db.execute(
        select(OutboundWebhook).where(
            OutboundWebhook.business_id == business_id,
            OutboundWebhook.active.is_(True),
        )
    )
    targets = [w for w in result.scalars().all() if event_type in w.event_types]

    for webhook in targets:
        await queue.enqueue(
            QUEUE,
            {
                "webhook_id": str(webhook.id),
                "event_type": event_type,
                "payload": payload,
            },
            db,
        )

    if targets:
        logger.info(
            "webhook dispatch queued business=%s event=%s targets=%s",
            business_id, event_type, len(targets),
        )
    return len(targets)
