"""
The COD voice-call failsafe: an unanswered WhatsApp COD confirmation
(shared/care/cod_confirmation.py + shared/scheduling/recall.py::
send_cod_confirmations) gets one automated phone call, not silence.

Credentials/from-number resolution mirrors shared/care/
escalation_failsafe.py exactly - same ChannelConnection(channel=voice)
lookup, same three fields read the same way. Reuses whatever voice number
is already connected for AI calls; a business with none simply never gets
this failsafe, same as the SMS one already degrades today.

Deliberately not the AI relay pipeline (shared/channels/voice/relay.py) -
a COD confirm/decline is a fixed yes/no, not a conversation, so this
places a call whose answer_url returns a plain Plivo <GetDigits> menu
(shared/channels/voice/cod_ivr.py), never opening the AI's <Stream>.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt
from shared.channels.voice import plivo_client
from shared.config.settings import settings
from shared.db.models import Business, Channel, ChannelConnection, ConnectionStatus, CustomerIdentity, IdentityKind, Order
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# How long the WhatsApp confirmation may go unanswered before the call
# failsafe fires - long enough that a customer replying within the hour
# isn't interrupted by a call, short enough the order isn't stuck all day.
_UNANSWERED_WINDOW = timedelta(hours=4)

# "Call placed but still unconfirmed" - the retries-exhausted case (no
# answer, or answered but no digits pressed) - escalates to staff after
# this, mirroring escalation_failsafe.py's own unacknowledged-window shape.
_CALL_NO_OUTCOME_WINDOW = timedelta(hours=1)


async def due_for_call(db: AsyncSession) -> int:
    """Place the failsafe call for every COD order whose WhatsApp
    confirmation has gone unanswered past the window. Returns how many
    calls were attempted (placed or skipped for a real reason)."""
    now = datetime.now(timezone.utc)
    cutoff = now - _UNANSWERED_WINDOW
    result = await db.execute(
        select(Order).where(
            Order.is_cod.is_(True),
            Order.cod_confirmation_sent_at.is_not(None),
            Order.cod_confirmation_sent_at <= cutoff,
            Order.cod_confirmed_at.is_(None),
            Order.cod_declined_at.is_(None),
            Order.cod_call_placed_at.is_(None),
        )
    )
    due = list(result.scalars().all())
    if not due:
        return 0

    base = settings.public_base_url.rstrip("/")
    for order in due:
        # Stamped regardless of outcome below - a business with no voice
        # number connected, or a real send failure, must not be retried
        # every sweep forever. Same reasoning as every other *_sent_at
        # column in this codebase.
        order.cod_call_placed_at = now

        business = await db.get(Business, order.business_id)
        if business is None or order.customer_id is None:
            continue

        to_number = (
            await db.execute(
                select(CustomerIdentity.value).where(
                    CustomerIdentity.customer_id == order.customer_id,
                    CustomerIdentity.kind == IdentityKind.phone,
                )
            )
        ).scalars().first()
        if not to_number:
            logger.info("COD call failsafe skipped order=%s - no phone on file", order.id)
            continue

        connection = (
            await db.execute(
                select(ChannelConnection).where(
                    ChannelConnection.business_id == order.business_id,
                    ChannelConnection.channel == Channel.voice,
                    ChannelConnection.status == ConnectionStatus.active,
                )
            )
        ).scalars().first()
        auth_id = (connection.extra or {}).get("subaccount_auth_id") if connection else None
        if connection is None or not connection.access_token or not auth_id:
            logger.info("COD call failsafe skipped order=%s - no voice number connected", order.id)
            continue

        try:
            await plivo_client.make_call(
                auth_id=auth_id,
                auth_token=decrypt(connection.access_token),
                from_number=connection.external_account_id,
                to_number=to_number,
                answer_url=f"{base}/voice/cod-answer?order_id={order.id}",
                hangup_url=f"{base}/voice/cod-hangup?order_id={order.id}",
            )
            logger.info("COD call failsafe placed order=%s business=%s", order.id, business.id)
        except plivo_client.PlivoError as exc:
            logger.warning("COD call failsafe failed order=%s: %s", order.id, exc)

    return len(due)


async def escalate_no_outcome(db: AsyncSession) -> int:
    """
    A call was placed but the order is still unconfirmed an hour later -
    no answer, or answered with no digits pressed (Plivo's own "retries
    exhausted, next XML element" behavior, which cod_ivr.py's answer
    route resolves to a plain hangup). Raises the normal escalation so a
    person follows up, same as a declined order already does.
    """
    from shared.ai import agent as agent_module

    now = datetime.now(timezone.utc)
    cutoff = now - _CALL_NO_OUTCOME_WINDOW
    result = await db.execute(
        select(Order).where(
            Order.is_cod.is_(True),
            Order.cod_call_placed_at.is_not(None),
            Order.cod_call_placed_at <= cutoff,
            Order.cod_call_escalated_at.is_(None),
            Order.cod_confirmed_at.is_(None),
            Order.cod_declined_at.is_(None),
        )
    )
    due = list(result.scalars().all())
    if not due:
        return 0

    for order in due:
        # A one-shot marker, same shape as Escalation.escalated_further_at -
        # this must fire exactly once, not every sweep, for an order that
        # stays unresolved forever.
        order.cod_call_escalated_at = now
        if order.customer_id is None:
            continue
        await agent_module.notify_escalation(
            order.business_id,
            reason=(
                f"COD confirmation call for order #{order.order_number or order.id} "
                "went unanswered - needs manual follow-up"
            ),
            customer_id=order.customer_id, channel="voice", db=db,
        )

    return len(due)
