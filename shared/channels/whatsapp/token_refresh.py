"""
Keeping client tokens alive.

Embedded Signup issues business integration system user tokens that expire
after 60 days. They are refreshed server-side; the client never re-authorises.

The failure this prevents is the quiet one. A client onboards, everything
works for two months, then their messages stop with no error anywhere. Because
expiry is measured from each client's own signup date, they fail one at a time
on different days, so it never looks like a systemic bug - it looks like each
customer individually going quiet.

Refresh happens well before expiry and the old token keeps working until its
original expiry, so a failed attempt has days of headroom rather than minutes.
"""

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt, encrypt
from shared.config.settings import settings
from shared.db.models import Channel, ChannelConnection, ConnectionStatus
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Refresh once a token is within this many days of expiring. Ten days of
# headroom means a Meta outage costs a retry, not a customer.
REFRESH_WINDOW = timedelta(days=10)

TOKEN_LIFETIME = timedelta(days=60)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def needs_refresh(connection: ChannelConnection, *, now: datetime | None = None) -> bool:
    """Whether this connection's token is close enough to expiry to renew."""
    if not connection.access_token:
        return False
    if connection.status != ConnectionStatus.active:
        return False
    if connection.token_expires_at is None:
        # Connected before we tracked expiry, or a permanent system user token.
        # Refreshing teaches us the real date, and is harmless if it does not
        # expire.
        return True

    expires = connection.token_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires - (now or _now()) <= REFRESH_WINDOW


async def refresh_connection(connection: ChannelConnection) -> bool:
    """
    Exchange this connection's token for a fresh 60-day one.

    On failure the existing token is left untouched - it is valid until its
    original expiry, which is the headroom that makes retrying safe.
    """
    try:
        current = decrypt(connection.access_token or "")
    except Exception:
        logger.exception("could not decrypt token for connection %s", connection.id)
        return False

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.get(
                f"{settings.graph_base_url}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": settings.meta_app_id,
                    "client_secret": settings.meta_app_secret,
                    "fb_exchange_token": current,
                    "set_token_expires_in_60_days": "true",
                },
            )
    except httpx.HTTPError as exc:
        logger.error("token refresh request failed connection=%s: %s", connection.id, exc)
        return False

    if response.status_code != 200:
        # Worth alerting on. A connection whose token cannot be refreshed will
        # go silent when the current one expires, and nothing else will say so.
        logger.error(
            "token refresh rejected connection=%s business=%s status=%s: %s",
            connection.id,
            connection.business_id,
            response.status_code,
            response.text[:300],
        )
        return False

    payload = response.json()
    token = payload.get("access_token")
    if not token:
        logger.error("token refresh returned no token connection=%s", connection.id)
        return False

    now = _now()
    expires_in = payload.get("expires_in")
    connection.access_token = encrypt(token)
    connection.token_issued_at = now
    connection.token_expires_at = (
        now + timedelta(seconds=int(expires_in)) if expires_in else now + TOKEN_LIFETIME
    )
    connection.token_refresh_failed_at = None

    logger.info(
        "token refreshed business=%s channel=%s expires=%s",
        connection.business_id,
        connection.channel,
        connection.token_expires_at.isoformat(),
    )
    return True


async def refresh_expiring(db: AsyncSession) -> dict[str, int]:
    """
    Renew every WhatsApp token close to expiring.

    Returns counts so the caller can log and alert. A non-zero `failed` is the
    signal that a client is heading for silence.
    """
    result = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.channel == Channel.whatsapp,
            ChannelConnection.status == ConnectionStatus.active,
        )
    )
    due = [c for c in result.scalars().all() if needs_refresh(c)]

    refreshed = failed = 0
    for connection in due:
        if await refresh_connection(connection):
            refreshed += 1
        else:
            failed += 1
            connection.token_refresh_failed_at = _now()
            # Mark it so the client sees "reconnect needed" rather than
            # discovering the problem when a customer gets no reply.
            if connection.token_expires_at and connection.token_expires_at <= _now():
                connection.status = ConnectionStatus.needs_reauth

    if due:
        await db.commit()

    if failed:
        logger.error(
            "token refresh: %s renewed, %s FAILED - those clients will go "
            "silent when their tokens expire",
            refreshed,
            failed,
        )
    elif refreshed:
        logger.info("token refresh: %s renewed", refreshed)

    return {"considered": len(due), "refreshed": refreshed, "failed": failed}
