"""
The second alarm: an Escalation nobody acknowledged within the window
gets one SMS to the business's own staff_phone_number, so it isn't just
sitting unread in a Slack channel someone muted.

Credentials/from-number resolution mirrors shared/channels/voice/
outbound.py's own build_context exactly (ChannelConnection.access_token
decrypted as the subaccount auth_token, connection.extra
["subaccount_auth_id"] as the auth_id, connection.external_account_id as
the from_number) - the same three fields, read the same way, not a
second lookup path for the same data.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt
from shared.channels.voice import plivo_client
from shared.db.models import Business, Channel, ChannelConnection, ConnectionStatus, Escalation
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# How long an escalation may sit unacknowledged before the SMS failsafe
# fires - generous enough that a person mid-task isn't paged for every
# single escalation, tight enough that a real gap doesn't sit all day.
_UNACKNOWLEDGED_WINDOW = timedelta(minutes=15)


async def check_unacknowledged(db: AsyncSession) -> int:
    """Fire the SMS failsafe for every Escalation past the window with no
    acknowledgement yet. Returns how many were processed (sent or skipped)."""
    cutoff = datetime.now(timezone.utc) - _UNACKNOWLEDGED_WINDOW
    result = await db.execute(
        select(Escalation).where(
            Escalation.acknowledged_at.is_(None),
            Escalation.escalated_further_at.is_(None),
            Escalation.created_at <= cutoff,
        )
    )
    due = list(result.scalars().all())
    if not due:
        return 0

    now = datetime.now(timezone.utc)
    for escalation in due:
        # Stamped regardless of outcome below - a business with no voice
        # number connected, or a real send failure, must not be retried
        # every 5 minutes forever. Failure is logged loudly instead.
        escalation.escalated_further_at = now

        business = await db.get(Business, escalation.business_id)
        if business is None:
            continue

        connection = (
            await db.execute(
                select(ChannelConnection).where(
                    ChannelConnection.business_id == escalation.business_id,
                    ChannelConnection.channel == Channel.voice,
                    ChannelConnection.status == ConnectionStatus.active,
                )
            )
        ).scalars().first()
        if connection is None or not connection.access_token:
            logger.info("escalation failsafe skipped id=%s - no voice number connected", escalation.id)
            continue

        staff_phone = (connection.extra or {}).get("staff_phone_number")
        auth_id = (connection.extra or {}).get("subaccount_auth_id")
        if not staff_phone or not auth_id:
            logger.info("escalation failsafe skipped id=%s - no staff_phone_number configured", escalation.id)
            continue

        try:
            await plivo_client.send_sms(
                auth_id=auth_id, auth_token=decrypt(connection.access_token),
                from_number=connection.external_account_id, to_number=staff_phone,
                text=f"Krova: unacknowledged escalation at {business.name} - {escalation.reason[:200]}",
            )
            logger.info("escalation failsafe SMS sent id=%s business=%s", escalation.id, business.id)
        except plivo_client.PlivoError as exc:
            logger.warning("escalation failsafe SMS failed id=%s: %s", escalation.id, exc)

    return len(due)
