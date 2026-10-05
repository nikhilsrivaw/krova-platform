"""
The Instagram username behind a customer's IGSID.

Direct messages only carry the numeric IGSID. Meta's conversations listing names each
participant with their username, so the username is found by matching the IGSID there.
Best-effort: a failure returns None and the escalation keeps the numeric id.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.channels.instagram.client import InstagramApiError, InstagramClient
from shared.db.models.channel import Channel, ChannelConnection
from shared.utils.logging import get_logger

logger = get_logger(__name__)


async def resolve_username(db: AsyncSession, business_id: uuid.UUID, igsid: str) -> str | None:
    try:
        connection = (await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == business_id,
                ChannelConnection.channel == Channel.instagram,
            ).limit(1)
        )).scalars().first()
        if connection is None:
            return None
        client = InstagramClient.for_connection(connection)
        for conversation in await client.list_conversations(limit=100):
            for participant in conversation.participants:
                if participant.id == igsid and participant.username:
                    return participant.username
    except InstagramApiError:
        logger.info("instagram username lookup refused for igsid=%s", igsid)
    except Exception:
        logger.exception("instagram username lookup failed for igsid=%s", igsid)
    return None
