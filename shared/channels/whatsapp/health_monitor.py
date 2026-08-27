"""
Watching a connected number's health, instead of only being able to look it
up.

AccountClient.health() and .readiness() (shared/channels/whatsapp/account.py)
read quality_rating, messaging_limit_tier and Meta's own can_send_message
blockers - real and thorough, and, until this, only ever called on demand
from a settings page a business has to think to open. Account.py's own
docstring calls this "the early warning nobody watches" - quality falls
before Meta restricts a number, and restriction happens well before messages
visibly stop, so a business that never opens that page finds out the same
way every competitor's customer does: customers stop replying and nobody
knows why.

Alerts only on a transition, never on every poll. The previous result is
kept on ChannelConnection.extra so "still RED" does not create a new
Insight every run - only the change that matters: healthy -> worse, or
worse -> recovered.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt
from shared.channels.whatsapp.account import AccountClient, AccountError
from shared.db.models import Channel, ChannelConnection, ConnectionStatus, Insight
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# GREEN is healthy; unrated (a brand new number) is treated as fine rather
# than scary - alerting on "unrated" would fire for every fresh connection.
_RATING_RANK = {"GREEN": 0, "YELLOW": 1, "RED": 2}


def _rank(rating: str | None) -> int:
    return _RATING_RANK.get(rating or "", 0)


def _label(connection: ChannelConnection) -> str:
    return connection.external_handle or connection.external_account_id


async def check_connection(connection: ChannelConnection, db: AsyncSession) -> None:
    if not connection.access_token:
        return

    client = AccountClient(decrypt(connection.access_token), connection.external_account_id)
    try:
        health = await client.health()
        readiness = await client.readiness()
    except AccountError as exc:
        logger.warning("health check failed connection=%s: %s", connection.id, exc)
        return

    previous = (connection.extra or {}).get("health") or {}
    previous_rating = previous.get("quality_rating")
    previous_can_send = previous.get("can_send")
    now = datetime.now(timezone.utc)

    connection.extra = {
        **(connection.extra or {}),
        "health": {
            "quality_rating": health.quality_rating,
            "messaging_limit_tier": health.messaging_limit_tier,
            "can_send": readiness.can_send,
            "checked_at": now.isoformat(),
        },
    }

    if _rank(health.quality_rating) > _rank(previous_rating):
        db.add(Insight(
            business_id=connection.business_id,
            kind="account_health",
            title=f"WhatsApp number quality dropped to {health.quality_rating}",
            body=(
                f"{_label(connection)}'s quality rating fell from {previous_rating or 'unrated'} "
                f"to {health.quality_rating}. Meta restricts messaging volume before a number "
                "stops sending entirely - worth checking what's driving complaints or blocks "
                "before it gets worse."
            ),
            severity="critical" if health.quality_rating == "RED" else "warning",
            created_at=now,
        ))
        logger.warning(
            "quality rating worsened business=%s %s -> %s",
            connection.business_id, previous_rating, health.quality_rating,
        )
    elif _rank(health.quality_rating) < _rank(previous_rating) and previous_rating:
        db.add(Insight(
            business_id=connection.business_id,
            kind="account_health",
            title=f"WhatsApp number quality recovered to {health.quality_rating}",
            body=f"{_label(connection)}'s quality rating improved from {previous_rating} to {health.quality_rating}.",
            severity="info",
            created_at=now,
        ))

    if readiness.can_send != "AVAILABLE" and previous_can_send in (None, "AVAILABLE"):
        reasons = "; ".join(b.message for b in readiness.blockers) or "Meta gave no specific reason"
        fix = next((b.fix for b in readiness.blockers if b.fix), None)
        db.add(Insight(
            business_id=connection.business_id,
            kind="account_health",
            title=f"WhatsApp number can no longer send freely ({readiness.can_send})",
            body=reasons + (f" Fix: {fix}" if fix else ""),
            severity="critical",
            created_at=now,
        ))
        logger.warning(
            "connection blocked business=%s can_send=%s", connection.business_id, readiness.can_send,
        )
    elif readiness.can_send == "AVAILABLE" and previous_can_send not in (None, "AVAILABLE"):
        db.add(Insight(
            business_id=connection.business_id,
            kind="account_health",
            title="WhatsApp number can send again",
            body=f"Whatever was blocking {_label(connection)} has cleared.",
            severity="info",
            created_at=now,
        ))

    await db.flush()


async def check_all(db: AsyncSession) -> int:
    """Check every active WhatsApp connection across every business. Returns how many were checked."""
    result = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.channel == Channel.whatsapp,
            ChannelConnection.status == ConnectionStatus.active,
        )
    )
    connections = result.scalars().all()
    checked = 0
    for connection in connections:
        try:
            await check_connection(connection, db)
            checked += 1
        except Exception:
            # One tenant's transient failure must never stop the rest of the
            # sweep - the whole point of this job is that nobody is watching.
            logger.exception("health check crashed connection=%s", connection.id)
    return checked
