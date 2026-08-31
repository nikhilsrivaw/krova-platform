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

WhatsApp only, matching approve()'s own existing scope before this
extraction - Instagram sending exists (shared/channels/instagram/client.py)
but drafts don't yet carry enough to route between channels here, and this
is a refactor preserving current behavior, not a channel expansion.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt
from shared.channels import ingest
from shared.channels.whatsapp.client import WhatsAppClient, WhatsAppError
from shared.db.models import (
    Channel,
    ChannelConnection,
    ConnectionStatus,
    CustomerIdentity,
    Direction,
    DraftStatus,
    IdentityKind,
    MessageDraft,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)


class DraftSendError(Exception):
    """The draft could not be sent. The message is safe to show a person."""


@dataclass(slots=True)
class DraftSendResult:
    draft: MessageDraft
    message_id: uuid.UUID | None


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

    phone_row = await db.execute(
        select(CustomerIdentity.value).where(
            CustomerIdentity.customer_id == draft.customer_id,
            CustomerIdentity.kind == IdentityKind.phone,
        )
    )
    to = phone_row.scalars().first()
    if not to:
        raise DraftSendError("No phone number on file for this customer")

    connection = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == business_id,
                ChannelConnection.channel == Channel.whatsapp,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()
    if connection is None or not connection.access_token:
        raise DraftSendError("WhatsApp is not connected")

    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)
    try:
        sent = await client.send_text(to, text)
    except WhatsAppError as exc:
        raise DraftSendError(str(exc)) from exc

    now = datetime.now(timezone.utc)
    stored = await ingest.ingest(
        business_id=business_id,
        channel=Channel.whatsapp,
        direction=Direction.outbound,
        identity_kind=IdentityKind.phone,
        identity_value=to,
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
        "draft sent business=%s draft=%s auto=%s",
        business_id, draft.id, reviewed_by_user_id is None,
    )
    return DraftSendResult(draft=draft, message_id=draft.sent_message_id)
