"""
Sending a carousel the business already built - one place for every caller.

Two callers need exactly the same thing: an automation rule's send_carousel
step (shared/care/post_call_actions.py) and an AI reply that chose to share a
carousel (shared/channels/send_draft.py). They used to each carry their own
copy of the Instagram half; the WhatsApp half is new and would have been a
third. One module keeps "what counts as sendable" in one place - including
for the list the AI is offered, so it is never shown a carousel that this
code would then refuse to send.

  instagram - a saved InstagramCarousel, sent as a Generic Template. No Meta
              review, but Instagram only delivers inside the 24-hour window.
  whatsapp  - an APPROVED carousel message template. A template can go out
              any time, but Meta charges for it (marketing most of all).

Every function here returns True/False and never raises for an ordinary
"can't send this" - the callers either already sent the reply it rides on
(AI) or log the outcome per step (automations), and neither should be broken
by a carousel that could not go.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt
from shared.channels import ingest
from shared.channels.instagram.client import (
    GenericTemplateButton,
    GenericTemplateElement,
    InstagramClient,
    InstagramSendError,
)
from shared.channels.whatsapp.carousel_media import usable_whatsapp_carousel_media
from shared.channels.whatsapp.client import CarouselSendCard, WhatsAppClient, WhatsAppError
from shared.db.models import (
    Channel,
    ChannelConnection,
    ConnectionStatus,
    CustomerIdentity,
    Direction,
    IdentityKind,
    InstagramCarousel,
    MessageTemplate,
    TemplateStatus,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)


async def _identity(customer_id: uuid.UUID, kind: IdentityKind, db: AsyncSession) -> str | None:
    return (
        await db.execute(
            select(CustomerIdentity.value).where(
                CustomerIdentity.customer_id == customer_id, CustomerIdentity.kind == kind,
            )
        )
    ).scalars().first()


async def _connection(business_id: uuid.UUID, channel: Channel, db: AsyncSession) -> ChannelConnection | None:
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
        return None
    return connection


async def send_instagram_carousel(
    business_id: uuid.UUID, customer_id: uuid.UUID, carousel: InstagramCarousel, db: AsyncSession,
) -> bool:
    connection = await _connection(business_id, Channel.instagram, db)
    if connection is None:
        logger.info("carousel not sent business=%s: no Instagram connection", business_id)
        return False

    to = await _identity(customer_id, IdentityKind.instagram, db)
    if not to:
        logger.info("carousel not sent business=%s customer=%s: no Instagram id on file", business_id, customer_id)
        return False

    client = InstagramClient.for_connection(connection)
    elements = [
        GenericTemplateElement(
            title=e.get("title", ""), subtitle=e.get("subtitle"), image_url=e.get("image_url"),
            buttons=[GenericTemplateButton(**b) for b in e.get("buttons", [])],
        )
        for e in (carousel.elements or [])
    ]
    try:
        sent = await client.send_generic_template(to, elements)
    except InstagramSendError:
        logger.warning(
            "carousel send failed business=%s customer=%s carousel=%s",
            business_id, customer_id, carousel.name, exc_info=True,
        )
        return False

    await ingest.ingest(
        business_id=business_id, channel=Channel.instagram, direction=Direction.outbound,
        identity_kind=IdentityKind.instagram, identity_value=to,
        external_id=sent.external_id or None, text=f"[Carousel: {carousel.name}]",
        occurred_at=datetime.now(timezone.utc), connection_id=connection.id,
        enqueue_analysis=False, db=db,
    )
    return True


async def send_whatsapp_carousel(
    business_id: uuid.UUID, customer_id: uuid.UUID, template: MessageTemplate, db: AsyncSession,
) -> bool:
    media_ids = usable_whatsapp_carousel_media(template)
    if media_ids is None:
        logger.warning(
            "carousel not sent business=%s: template %r is not an approved carousel without variables",
            business_id, getattr(template, "name", None),
        )
        return False

    phone = await _identity(customer_id, IdentityKind.phone, db)
    if phone is None:
        return False
    connection = await _connection(business_id, Channel.whatsapp, db)
    if connection is None:
        return False

    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)
    try:
        sent = await client.send_template(
            phone, template.name, template.language,
            carousel_cards=[CarouselSendCard(media_id=m) for m in media_ids],
        )
    except WhatsAppError:
        logger.warning(
            "carousel send failed business=%s customer=%s template=%s",
            business_id, customer_id, template.name, exc_info=True,
        )
        return False

    await ingest.ingest(
        business_id=business_id, channel=Channel.whatsapp, direction=Direction.outbound,
        identity_kind=IdentityKind.phone, identity_value=phone,
        external_id=sent.external_id, text=template.body_text or f"[Carousel: {template.name}]",
        occurred_at=datetime.now(timezone.utc), connection_id=connection.id,
        enqueue_analysis=False, db=db,
    )
    return True


async def share_named_carousel(
    *, business_id: uuid.UUID, customer_id: uuid.UUID, channel: str, name: str, db: AsyncSession,
) -> bool:
    """
    Send the carousel the AI named, looked up in the table for the channel the
    conversation is on. A name that does not exist there - the model naming an
    Instagram carousel on WhatsApp, or inventing one - is a logged no-op,
    never a customer-visible failure: the reply it rides on already sent.
    """
    if channel == Channel.instagram.value:
        carousel = (
            await db.execute(
                select(InstagramCarousel).where(
                    InstagramCarousel.business_id == business_id, InstagramCarousel.name == name,
                )
            )
        ).scalars().first()
        if carousel is None:
            logger.warning("share_carousel named unknown Instagram carousel=%r business=%s", name, business_id)
            return False
        return await send_instagram_carousel(business_id, customer_id, carousel, db)

    if channel == Channel.whatsapp.value:
        candidates = (
            await db.execute(
                select(MessageTemplate).where(
                    MessageTemplate.business_id == business_id,
                    MessageTemplate.name == name,
                    MessageTemplate.status == TemplateStatus.approved,
                )
            )
        ).scalars().all()
        # The same name can exist in several languages; send the first one that
        # is actually sendable, English before the rest.
        usable = sorted(
            (t for t in candidates if usable_whatsapp_carousel_media(t) is not None),
            key=lambda t: (t.language != "en", t.language),
        )
        if not usable:
            logger.warning("share_carousel named unknown/unsendable WhatsApp carousel=%r business=%s", name, business_id)
            return False
        return await send_whatsapp_carousel(business_id, customer_id, usable[0], db)

    logger.info("share_carousel ignored: carousels are not supported on channel=%s", channel)
    return False
