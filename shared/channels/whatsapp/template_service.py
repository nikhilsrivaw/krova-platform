"""
Submitting a template to Meta and recording it - one place for every caller.

The Templates page and the "create the templates this feature needs" button
both do exactly this, so it lives here rather than being copied: encrypt-free
connection lookup, the Meta call, and the local mirror row.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt
from shared.channels.whatsapp import templates as meta
from shared.db.models import (
    Channel,
    ChannelConnection,
    ConnectionStatus,
    MessageTemplate,
    TemplateCategory,
    TemplateStatus,
)


class WhatsAppNotReady(Exception):
    """No usable WhatsApp connection to submit templates through. Safe to show as-is."""


async def get_connection(db: AsyncSession, business_id: uuid.UUID) -> tuple[ChannelConnection, str]:
    """The business's active WhatsApp connection and its WABA id."""
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
        raise WhatsAppNotReady("Connect WhatsApp before creating templates")
    waba_id = (connection.extra or {}).get("waba_id")
    if not waba_id:
        raise WhatsAppNotReady("This WhatsApp connection is incomplete. Please reconnect.")
    return connection, waba_id


async def submit(
    db: AsyncSession,
    *,
    business_id: uuid.UUID,
    connection: ChannelConnection,
    waba_id: str,
    draft: meta.TemplateDraft,
    extra: dict | None = None,
) -> MessageTemplate:
    """
    Submit `draft` to Meta for review and mirror it locally as PENDING (or
    APPROVED if Meta says so at once). Raises meta.TemplateError if Meta
    refuses it - nothing is written in that case.
    """
    name = meta.normalise_name(draft.name)
    result = await meta.TemplateClient(decrypt(connection.access_token), waba_id).create(draft)

    template = MessageTemplate(
        business_id=business_id,
        connection_id=connection.id,
        external_id=result.get("id"),
        name=name,
        language=draft.language,
        category=TemplateCategory(draft.category),
        # Meta returns its own status; anything other than APPROVED starts
        # as pending review.
        status=TemplateStatus.approved if result.get("status") == "APPROVED" else TemplateStatus.pending,
        components=draft.to_components(),
        body_text=draft.body,
        submitted_at=datetime.now(timezone.utc),
        extra=extra or {},
    )
    db.add(template)
    await db.flush()
    return template
