"""
Bulk messages from an owner command, through the same WhatsApp primitives the
inbox uses. Two modes, both confirmed first:

- template: an approved template to any chosen customer. Works outside the
  24-hour window, which is the only way to start a conversation.
- window_text: free text, only to customers who wrote in the last 24 hours.
  Anyone outside the window is skipped with a reason, never sent a text that
  Meta would refuse.

Every outbound message is written to the conversation, so the owner's inbox
shows what was sent.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt
from shared.channels import ingest
from shared.channels.whatsapp.client import WhatsAppClient, WhatsAppError, within_service_window
from shared.commands.tools import ToolRefused
from shared.db.models import (
    Business, Channel, ChannelConnection, ConnectionStatus, Customer, CustomerIdentity,
    Direction, IdentityKind, Message, MessageTemplate, TemplateStatus,
)

MAX_RECIPIENTS = 50


async def _connection(db: AsyncSession, business: Business) -> ChannelConnection:
    row = (await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == business.id,
            ChannelConnection.channel == Channel.whatsapp,
            ChannelConnection.status == ConnectionStatus.active,
        )
    )).scalars().first()
    if row is None or not row.access_token:
        raise ToolRefused("WhatsApp abhi connect nahi hai")
    return row


async def _phone_and_last_inbound(
    db: AsyncSession, business: Business, customer: Customer,
) -> tuple[str | None, datetime | None]:
    phone = (await db.execute(
        select(CustomerIdentity.value).where(
            CustomerIdentity.business_id == business.id,
            CustomerIdentity.customer_id == customer.id,
            CustomerIdentity.kind == IdentityKind.phone,
        )
    )).scalars().first()
    last = (await db.execute(
        select(Message.occurred_at).where(
            Message.customer_id == customer.id, Message.direction == Direction.inbound,
        ).order_by(Message.occurred_at.desc()).limit(1)
    )).scalars().first()
    return phone, last


async def _customers(db: AsyncSession, business: Business, ids: list[str]) -> list[Customer]:
    try:
        uuids = [uuid.UUID(i) for i in ids]
    except ValueError as exc:
        raise ToolRefused("Customer ka id galat hai") from exc
    rows = (await db.execute(
        select(Customer).where(Customer.business_id == business.id, Customer.id.in_(uuids))
    )).scalars().all()
    found = {c.id for c in rows}
    missing = [str(u) for u in uuids if u not in found]
    if missing:
        raise ToolRefused(f"{len(missing)} customer aapke business mein nahi mile")
    return rows


async def _template(db: AsyncSession, business: Business, name: str, language: str) -> MessageTemplate:
    template = (await db.execute(
        select(MessageTemplate).where(
            MessageTemplate.business_id == business.id,
            MessageTemplate.name == name, MessageTemplate.language == language,
        )
    )).scalars().first()
    if template is None:
        raise ToolRefused(f"'{name}' naam ka template nahi mila")
    if template.status != TemplateStatus.approved:
        raise ToolRefused(f"'{name}' template abhi approved nahi hai")
    return template


async def send_bulk(db: AsyncSession, business: Business, user_id: uuid.UUID, args: dict[str, Any]) -> dict[str, Any]:
    ids = args["customer_ids"]
    if not ids or len(ids) > MAX_RECIPIENTS:
        raise ToolRefused(f"1 se {MAX_RECIPIENTS} customers chuniye")
    customers = await _customers(db, business, ids)
    connection = await _connection(db, business)
    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)

    template = None
    if args["mode"] == "template":
        template = await _template(db, business, args["template_name"], args.get("language", "en"))
    body = (args.get("body") or "").strip()
    if args["mode"] == "window_text" and not body:
        raise ToolRefused("Text khaali nahi ho sakta")

    sent: list[str] = []
    skipped: list[dict[str, str]] = []
    now = datetime.now(timezone.utc)

    for customer in customers:
        phone, last_inbound = await _phone_and_last_inbound(db, business, customer)
        if phone is None:
            skipped.append({"customer_id": str(customer.id), "reason": "phone number nahi hai"})
            continue
        if customer.is_private:
            skipped.append({"customer_id": str(customer.id), "reason": "private conversation"})
            continue

        try:
            if template is not None:
                result = await client.send_template(
                    phone, template.name, template.language, body_params=args.get("variables") or None,
                )
                text = template.body_text or template.name
                for i, value in enumerate(args.get("variables") or [], start=1):
                    text = text.replace(f"{{{{{i}}}}}", value)
            else:
                if not within_service_window(last_inbound):
                    skipped.append({"customer_id": str(customer.id), "reason": "24 ghante ki window band hai"})
                    continue
                result = await client.send_text(phone, body)
                text = body
        except WhatsAppError as exc:
            skipped.append({"customer_id": str(customer.id), "reason": str(exc)[:120]})
            continue

        await ingest.ingest(
            business_id=business.id, channel=Channel.whatsapp, direction=Direction.outbound,
            identity_kind=IdentityKind.phone, identity_value=phone, external_id=result.external_id,
            text=text, occurred_at=now, connection_id=connection.id, enqueue_analysis=False,
            sent_by_user_id=user_id, db=db,
        )
        sent.append(str(customer.id))

    return {"sent": len(sent), "sent_customer_ids": sent, "skipped": skipped}
