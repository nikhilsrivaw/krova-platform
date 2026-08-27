"""
Turning a channel event into a stored conversation.

Every channel funnels through here. A WhatsApp webhook, an Instagram DM, a
fetched email and a finished call turn all arrive as the same three questions:
which business, which human, what was said. Answer those and the rest of the
platform - extraction, the timeline, the ledger - works without knowing where
the message came from.

Deliberately not channel-aware beyond its arguments. The day voice writes its
transcript turns through this function is the day commitment extraction starts
covering phone calls with no extra code.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.billing import usage
from shared.db import queue
from shared.db.models import (
    Channel,
    ChannelConnection,
    Customer,
    Direction,
    IdentityKind,
    Message,
    UsageEventType,
)
from shared.identity import resolver
from shared.identity.normalise import InvalidIdentifier
from shared.utils.logging import get_logger

logger = get_logger(__name__)

ANALYSE_QUEUE = "analyse_message"
DRAFT_QUEUE = "draft_reply"


@dataclass(slots=True)
class Ingested:
    message: Message | None
    customer: Customer | None
    created: bool          # False when this was a duplicate delivery
    reason: str | None = None


async def find_connection(
    channel: Channel | str, external_account_id: str, db: AsyncSession
) -> ChannelConnection | None:
    """
    Which business owns the account this arrived on.

    For WhatsApp that is the phone_number_id from the webhook's metadata -
    the number that received the message, which is how one webhook endpoint
    serves every tenant.
    """
    channel_value = channel.value if isinstance(channel, Channel) else str(channel)
    result = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.channel == channel_value,
            ChannelConnection.external_account_id == external_account_id,
        )
    )
    return result.scalar_one_or_none()


async def ingest(
    *,
    business_id: uuid.UUID,
    channel: Channel,
    direction: Direction,
    identity_kind: IdentityKind,
    identity_value: str,
    external_id: str | None,
    text: str | None,
    occurred_at: datetime,
    db: AsyncSession,
    connection_id: uuid.UUID | None = None,
    subject: str | None = None,
    display_name: str | None = None,
    media: dict[str, Any] | None = None,
    raw: dict[str, Any] | None = None,
    enqueue_analysis: bool = True,
    referral: dict[str, Any] | None = None,
) -> Ingested:
    """
    Store one message, attributing it to a customer.

    Idempotent on (business_id, external_id): Meta retries deliveries, and a
    duplicate row here means answering the same customer twice.

    `referral` is Click-to-WhatsApp ad metadata, present only on the first
    message after someone taps an ad - captured onto the customer once and
    never overwritten, since Meta never repeats it on later messages.
    """
    if external_id:
        existing = await db.execute(
            select(Message).where(
                Message.business_id == business_id, Message.external_id == external_id
            )
        )
        found = existing.scalar_one_or_none()
        if found is not None:
            logger.debug("duplicate delivery of %s ignored", external_id)
            return Ingested(message=found, customer=None, created=False, reason="duplicate")

    try:
        resolution = await resolver.resolve(
            business_id, identity_kind, identity_value, db, display_name=display_name
        )
    except InvalidIdentifier as exc:
        # No usable identifier means no customer to attribute this to. Better
        # to drop it loudly than to invent an anonymous customer that quietly
        # accumulates other people's messages.
        logger.warning("cannot attribute message %s: %s", external_id, exc)
        return Ingested(message=None, customer=None, created=False, reason=str(exc))

    customer = resolution.customer

    if referral and referral.get("ctwa_clid") and customer.ctwa_clid is None:
        customer.ctwa_clid = referral.get("ctwa_clid")
        customer.ctwa_source_id = referral.get("source_id")
        customer.ctwa_headline = referral.get("headline")
        customer.ctwa_captured_at = datetime.now(timezone.utc)
        logger.info(
            "ctwa attribution captured customer=%s source_id=%s",
            customer.id, customer.ctwa_source_id,
        )

    message = Message(
        business_id=business_id,
        customer_id=customer.id,
        connection_id=connection_id,
        channel=channel,
        direction=direction,
        external_id=external_id,
        content=text,
        subject=subject,
        media=media or {},
        occurred_at=occurred_at,
        raw_payload=raw or {},
        created_at=datetime.now(timezone.utc),
    )
    db.add(message)

    try:
        await db.flush()
    except IntegrityError:
        # The same delivery arriving twice concurrently. The unique index is
        # the real guard; the check above is only an optimisation.
        await db.rollback()
        again = await db.execute(
            select(Message).where(
                Message.business_id == business_id, Message.external_id == external_id
            )
        )
        duplicate = again.scalar_one_or_none()
        if duplicate is None:
            raise
        return Ingested(message=duplicate, customer=None, created=False, reason="duplicate")

    if customer.last_contact_at is None or occurred_at > customer.last_contact_at:
        customer.last_contact_at = occurred_at

    # Inbound only, matching "webhook volume" the way AiSensy/Interakt/
    # Gupshup meter it - an inbound message is what actually arrives as a
    # webhook and costs Krova a processing cycle; an outbound send is
    # already counted wherever it was generated (an AI reply, a campaign
    # send), so counting it again here would double it.
    if direction == Direction.inbound:
        usage.record(
            business_id=business_id,
            event_type=UsageEventType.message_processed,
            channel=channel.value if isinstance(channel, Channel) else str(channel),
            quantity=1,
            unit="message",
            source_type="message",
            source_id=message.id,
            occurred_at=occurred_at,
            db=db,
        )

    if enqueue_analysis and not customer.is_private:
        # A customer the owner marked private is stored but never analysed and
        # never answered. That is what makes "this thread is personal" a real
        # guarantee rather than a promise, and it is why reading a mixed
        # Instagram inbox is defensible at all.
        await queue.enqueue(
            ANALYSE_QUEUE, {"message_id": str(message.id)}, db
        )
        # Inbound messages also get a reply drafted. Separate queue, because
        # extraction can take its time and a reply cannot - a customer waiting
        # behind a nightly job has already gone elsewhere.
        if direction == Direction.inbound:
            await queue.enqueue(
                DRAFT_QUEUE, {"message_id": str(message.id)}, db
            )

    return Ingested(message=message, customer=customer, created=True)
