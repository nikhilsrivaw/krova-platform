"""
The ongoing half of Instagram messaging - a periodic pull that fills the
gap when Meta's push webhook isn't delivering.

Meta's `messages` webhook is the only intended path for inbound Instagram
DMs, but confirmed live: while instagram_manage_messages/
instagram_business_manage_messages is still pending App Review, Meta can
silently stop delivering that push even when every subscription check
(the connected page's own subscribed_apps, and the app's own Webhooks
product config) reports correctly configured, active, pointed at the
right URL. The same conversation is still fully readable through the
Conversations API used here - it's only the push that's gated, not the
data. This module reads what the push would have delivered.

Mirrors shared/channels/email/backfill.py's sync_all_active shape: a
short, cheap, repeatable read per connection, deduped through ingest()'s
own uniqueness on (business_id, external_id) - an already-stored message
is just skipped, never duplicated, so re-scanning the same handful of
recent conversations every run is safe. Unlike Gmail's sync, this keeps
ingest()'s default enqueue_analysis=True: a message caught by this sweep
should behave exactly as if the webhook had delivered it, automations
and draft replies included - the whole point is making the two paths
indistinguishable to the rest of the platform.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.channels import ingest
from shared.channels.instagram.client import (
    Conversation, ConversationMessage, InstagramApiError, InstagramClient,
)
from shared.db.models import Channel, ChannelConnection, ConnectionStatus, Direction, IdentityKind
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# How many of a connected account's most recent conversations to check
# each sweep, and how many of each conversation's most recent messages -
# generous enough to never miss a burst of testing, cheap enough to run
# every second across every connected account.
CONVERSATION_LIMIT = 10
MESSAGES_PER_CONVERSATION = 10


async def _read_conversation(
    client: InstagramClient, connection: ChannelConnection, conversation: Conversation
) -> list[ConversationMessage]:
    try:
        return await client.list_messages(conversation.id, limit=MESSAGES_PER_CONVERSATION)
    except InstagramApiError:
        logger.exception(
            "instagram sync could not read conversation=%s business=%s",
            conversation.id, connection.business_id,
        )
        return []


async def sync_connection(connection: ChannelConnection, db: AsyncSession) -> int:
    """
    Pull whatever's new across this one connected account's recent
    conversations. Returns how many new messages were stored.

    Reading each conversation's messages is a Graph API round trip with
    real network latency - at a 1-second poll interval that adds up fast
    if done one conversation at a time (APScheduler won't start the next
    tick until this one returns). Those reads touch nothing but the Graph
    API, so they run concurrently via asyncio.gather; only the ingest()
    calls after - which write through the one shared `db` session, unsafe
    to interleave - stay sequential.
    """
    client = InstagramClient.for_connection(connection)
    stored = 0

    try:
        conversations = [
            c for c in await client.list_conversations(limit=CONVERSATION_LIMIT)
            if c.participants
        ]
    except InstagramApiError:
        logger.exception(
            "instagram sync could not list conversations business=%s",
            connection.business_id,
        )
        return 0

    if not conversations:
        return 0

    per_conversation_messages = await asyncio.gather(
        *(_read_conversation(client, connection, c) for c in conversations)
    )

    for conversation, messages in zip(conversations, per_conversation_messages):
        # A DM thread has exactly one other participant - the customer.
        counterparty = conversation.participants[0].id

        for message in messages:
            if not message.text:
                # Meta returns story replies/reactions/attachments in this
                # same edge with no `message` text - nothing for the
                # automations engine or a customer timeline to act on yet.
                continue
            direction = (
                Direction.outbound if message.from_id == client.own_account_id
                else Direction.inbound
            )
            result = await ingest.ingest(
                business_id=connection.business_id,
                channel=Channel.instagram,
                direction=direction,
                identity_kind=IdentityKind.instagram,
                identity_value=counterparty,
                external_id=message.id,
                text=message.text,
                occurred_at=message.created_time,
                connection_id=connection.id,
                db=db,
            )
            if result.created:
                stored += 1

    return stored


async def sync_all_active(db: AsyncSession) -> int:
    """
    The scheduled sweep - see this module's own docstring for why it
    exists. One connection's failure (a revoked token, a Graph API
    hiccup) is logged and skipped, never stops another business's sync.
    """
    connections = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.channel == Channel.instagram,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().all()

    total_stored = 0
    for connection in connections:
        stored = await sync_connection(connection, db)
        total_stored += stored

    if total_stored:
        logger.info(
            "instagram sync stored %s new message(s) across %s connection(s)",
            total_stored, len(connections),
        )
    return total_stored
