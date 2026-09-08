"""
Turning an approved (or auto-approved) MessageDraft into a real outbound message.

One function, two callers: a human clicking Approve in the queue
(services/api/routers/approvals.py), and the draft_reply worker sending
without a human when a business has raised its autonomy to `act`
(services/workers/respond.py). Both need the exact same steps - look up the
customer's number, find the active WhatsApp connection, send, record the
outbound message, mark the draft sent - so this exists once rather than
twice, which is what let the two paths drift apart in the first place: `act`
mode has been a selectable setting since day one but never actually sent
anything, because nothing implemented its half of the contract.

WhatsApp and Instagram, matching what drafts actually carry: a draft's
in_reply_to_id points back at the Message it answers, and that Message's
own channel/media is enough to route both cases correctly - see the
per-channel helpers below.

Instagram is not one send shape, it's two, and they are not
interchangeable (confirmed against developers.facebook.com/docs/
messenger-platform/instagram/features/private-replies): a reply to a
*comment* must use Meta's private-reply contract (recipient.comment_id,
7-day window, one reply per comment ever) while a reply to a DM uses the
sender's own IGSID the normal way. Getting this wrong doesn't just fail
loudly - sending an IGSID-addressed message for what was actually a
comment reply is not a documented fallback, so the two paths are kept
strictly separate rather than one code path trying both.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt
from shared.channels import ingest
from shared.channels.instagram.client import InstagramClient, InstagramSendError
from shared.channels.whatsapp.client import WhatsAppClient, WhatsAppError
from shared.db.models import (
    Channel,
    ChannelConnection,
    ConnectionStatus,
    CustomerIdentity,
    Direction,
    DraftStatus,
    IdentityKind,
    Message,
    MessageDraft,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Meta's own private-reply limit - see this module's docstring.
_PRIVATE_REPLY_WINDOW = timedelta(days=7)


class DraftSendError(Exception):
    """The draft could not be sent. The message is safe to show a person."""


@dataclass(slots=True)
class DraftSendResult:
    draft: MessageDraft
    message_id: uuid.UUID | None


async def _active_connection(business_id: uuid.UUID, channel: Channel, db: AsyncSession) -> ChannelConnection:
    connection = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == business_id,
                ChannelConnection.channel == channel,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()
    if connection is None or not connection.access_token:
        raise DraftSendError(f"{channel.value.capitalize()} is not connected")
    return connection


async def _send_whatsapp(draft: MessageDraft, business_id: uuid.UUID, text: str, db: AsyncSession):
    phone_row = await db.execute(
        select(CustomerIdentity.value).where(
            CustomerIdentity.customer_id == draft.customer_id,
            CustomerIdentity.kind == IdentityKind.phone,
        )
    )
    to = phone_row.scalars().first()
    if not to:
        raise DraftSendError("No phone number on file for this customer")

    connection = await _active_connection(business_id, Channel.whatsapp, db)
    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)
    try:
        sent = await client.send_text(to, text)
    except WhatsAppError as exc:
        raise DraftSendError(str(exc)) from exc

    return sent, connection, IdentityKind.phone, to


async def _send_instagram(draft: MessageDraft, business_id: uuid.UUID, text: str, db: AsyncSession):
    source = await db.get(Message, draft.in_reply_to_id) if draft.in_reply_to_id else None
    media = (source.media or {}) if source is not None else {}
    connection = await _active_connection(business_id, Channel.instagram, db)
    client = InstagramClient.for_connection(connection)

    if media.get("kind") == "comment" and media.get("comment_id"):
        occurred_at = source.occurred_at
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - occurred_at > _PRIVATE_REPLY_WINDOW:
            raise DraftSendError(
                "The 7-day window to privately reply to this Instagram comment has "
                "closed - Meta will refuse this send."
            )
        try:
            sent = await client.send_private_reply(media["comment_id"], text)
        except InstagramSendError as exc:
            raise DraftSendError(str(exc)) from exc
        # A private reply is addressed by comment_id, not the commenter's
        # own identity value - identity_value still records who it reached,
        # read off the source message's own sender identity the same way
        # the DM branch below reads it, for a consistent outbound record.
        to = source.customer_id and (
            await db.execute(
                select(CustomerIdentity.value).where(
                    CustomerIdentity.customer_id == source.customer_id,
                    CustomerIdentity.kind == IdentityKind.instagram,
                )
            )
        ).scalars().first()
        return sent, connection, IdentityKind.instagram, (to or media["comment_id"])

    ig_row = await db.execute(
        select(CustomerIdentity.value).where(
            CustomerIdentity.customer_id == draft.customer_id,
            CustomerIdentity.kind == IdentityKind.instagram,
        )
    )
    to = ig_row.scalars().first()
    if not to:
        raise DraftSendError("No Instagram id on file for this customer")
    try:
        sent = await client.send_text(to, text)
    except InstagramSendError as exc:
        raise DraftSendError(str(exc)) from exc
    return sent, connection, IdentityKind.instagram, to


_SENDERS = {
    Channel.whatsapp.value: _send_whatsapp,
    Channel.instagram.value: _send_instagram,
}


async def send_draft(
    draft: MessageDraft,
    business_id: uuid.UUID,
    db: AsyncSession,
    *,
    reviewed_by_user_id: uuid.UUID | None,
) -> DraftSendResult:
    """
    Send a pending draft's final_body and mark it sent.

    `reviewed_by_user_id` is who approved it, when a person did - None means
    the business's own `act` autonomy setting approved it, not a person.
    Caller is responsible for status/expiry checks before calling this - see
    approve()'s own checks, which happen before this runs.
    """
    text = draft.final_body
    if not text:
        raise DraftSendError("There is nothing to send. Write a reply or reject this.")

    sender = _SENDERS.get(draft.channel)
    if sender is None:
        raise DraftSendError(f"Sending on {draft.channel} is not supported yet")

    sent, connection, identity_kind, identity_value = await sender(draft, business_id, text, db)

    now = datetime.now(timezone.utc)
    stored = await ingest.ingest(
        business_id=business_id,
        channel=Channel(draft.channel),
        direction=Direction.outbound,
        identity_kind=identity_kind,
        identity_value=identity_value,
        external_id=sent.external_id,
        text=text,
        occurred_at=now,
        connection_id=connection.id,
        enqueue_analysis=False,
        sent_by_user_id=reviewed_by_user_id,
        db=db,
    )

    draft.status = DraftStatus.sent
    draft.reviewed_by_user_id = reviewed_by_user_id
    draft.reviewed_at = now
    draft.sent_message_id = stored.message.id if stored.message else None

    logger.info(
        "draft sent business=%s draft=%s channel=%s auto=%s",
        business_id, draft.id, draft.channel, reviewed_by_user_id is None,
    )
    return DraftSendResult(draft=draft, message_id=draft.sent_message_id)
