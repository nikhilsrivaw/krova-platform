"""
The second alarm: an Escalation nobody acknowledged within the window
gets a real notification, so it isn't just sitting unread in a Slack
channel someone muted.

Two paths, tried in order, not both:

1. SMS to the business's own staff_phone_number, when a voice number is
   connected. Credentials/from-number resolution mirrors
   shared/channels/voice/outbound.py's own build_context exactly
   (ChannelConnection.access_token decrypted as the subaccount
   auth_token, connection.extra["subaccount_auth_id"] as the auth_id,
   connection.external_account_id as the from_number) - the same three
   fields, read the same way, not a second lookup path for the same data.
2. Email to the business's own owner, when path 1 isn't available (no
   voice connection, no staff_phone_number configured) OR the SMS send
   itself failed. This is the real fix for a confirmed coverage hole: a
   WhatsApp/Instagram/email-only business (never bought a voice number)
   previously had NO second-alarm mechanism at all - escalated_further_at
   still got stamped, so it silently never retried, either. Every
   business has at least one owner User with a real email - the one
   channel-agnostic contact guaranteed to exist with zero new
   per-business setup. Sent from settings.notifications_from_email, a
   Krova-owned address verified once in Krova's own Postmark account -
   never a business's own (possibly unset) EmailSendConnection, since
   this is an internal ops alert, not customer-facing mail.

Honest limitation, not hidden: email is a weaker-urgency channel than SMS
for something time-sensitive. This closes "zero coverage" for a real
majority of businesses, not "the ideal channel" - a future per-business
failsafe-contact override is a real, separate improvement if this proves
too weak in practice.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt
from shared.channels.voice import plivo_client
from shared.config.settings import settings
from shared.db.models import (
    Business,
    BusinessMember,
    BusinessRole,
    Channel,
    ChannelConnection,
    ConnectionStatus,
    Escalation,
    User,
)
from shared.integrations import postmark
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# How long an escalation may sit unacknowledged before the failsafe fires
# - generous enough that a person mid-task isn't paged for every single
# escalation, tight enough that a real gap doesn't sit all day.
_UNACKNOWLEDGED_WINDOW = timedelta(minutes=15)


async def check_unacknowledged(db: AsyncSession) -> int:
    """Fire the failsafe for every Escalation past the window with no
    acknowledgement yet. Returns how many were processed (notified or
    skipped either way)."""
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
        # Stamped regardless of outcome below - a business with no
        # reachable staff at all, or a real send failure on both paths,
        # must not be retried every 5 minutes forever. Failure is logged
        # loudly instead.
        escalation.escalated_further_at = now

        business = await db.get(Business, escalation.business_id)
        if business is None:
            continue

        if not await _try_sms(escalation, business, db):
            await _try_email_fallback(escalation, business, db)

    return len(due)


async def _try_sms(escalation: Escalation, business: Business, db: AsyncSession) -> bool:
    """Returns True if the SMS was actually sent."""
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
        logger.info("escalation failsafe: no voice number connected id=%s", escalation.id)
        return False

    staff_phone = (connection.extra or {}).get("staff_phone_number")
    auth_id = (connection.extra or {}).get("subaccount_auth_id")
    if not staff_phone or not auth_id:
        logger.info("escalation failsafe: no staff_phone_number configured id=%s", escalation.id)
        return False

    try:
        await plivo_client.send_sms(
            auth_id=auth_id, auth_token=decrypt(connection.access_token),
            from_number=connection.external_account_id, to_number=staff_phone,
            text=f"Krova: unacknowledged escalation at {business.name} - {escalation.reason[:200]}",
        )
        logger.info("escalation failsafe SMS sent id=%s business=%s", escalation.id, business.id)
        return True
    except plivo_client.PlivoError as exc:
        logger.warning("escalation failsafe SMS failed id=%s: %s - falling back to email", escalation.id, exc)
        return False


async def _try_email_fallback(escalation: Escalation, business: Business, db: AsyncSession) -> bool:
    """Returns True if the fallback email was actually sent."""
    if not settings.notifications_from_email:
        logger.info(
            "escalation failsafe: no notifications_from_email configured, cannot email-fallback id=%s",
            escalation.id,
        )
        return False

    owner_email = (
        await db.execute(
            select(User.email)
            .join(BusinessMember, BusinessMember.user_id == User.id)
            .where(BusinessMember.business_id == business.id, BusinessMember.role == BusinessRole.owner)
            .limit(1)
        )
    ).scalar_one_or_none()
    if not owner_email:
        logger.warning("escalation failsafe: no owner found to email id=%s business=%s", escalation.id, business.id)
        return False

    try:
        await postmark.send_email(
            from_email=settings.notifications_from_email,
            to=owner_email,
            subject=f"Krova: unacknowledged escalation at {business.name}",
            text_body=(
                f"An escalation at {business.name} has gone unacknowledged for over "
                f"{int(_UNACKNOWLEDGED_WINDOW.total_seconds() // 60)} minutes.\n\n"
                f"Reason: {escalation.reason}\n\n"
                "Sign in to KROVA to review and acknowledge it."
            ),
        )
        logger.info("escalation failsafe email sent id=%s business=%s", escalation.id, business.id)
        return True
    except postmark.PostmarkError as exc:
        logger.warning("escalation failsafe email failed id=%s: %s", escalation.id, exc)
        return False
