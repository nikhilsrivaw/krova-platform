"""
Shared lead intake for every listing platform (Justdial, IndiaMART, ...).

Each platform gives a per-business URL token and a parser for its own payload.
This module does the rest the same way for all of them: dedupe by the
platform's own lead id, link the phone to a customer, keep the enquiry as a CRM
note, and store the raw payload so a field mismatch is fixed from one row.

What this does NOT do: send a WhatsApp message to the lead. A lead has never
messaged the business, so the first message has to be an approved template.
"""

import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Business, CustomerNote, IdentityKind, InboundLead
from shared.identity import resolver
from shared.identity.normalise import InvalidIdentifier, normalise_phone
from shared.leads.justdial_parse import ParsedLead

PLATFORM_LABELS = {"justdial": "Justdial", "indiamart": "IndiaMART"}


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    # Only the hash is stored. A lost URL means generating a new one.
    return hashlib.sha256(token.encode()).hexdigest()


async def ingest_parsed(
    db: AsyncSession, business: Business, source: str, parsed: ParsedLead, payload: dict,
) -> InboundLead:
    now = datetime.now(timezone.utc)

    if parsed.external_id:
        already = (
            await db.execute(
                select(InboundLead).where(
                    InboundLead.business_id == business.id,
                    InboundLead.source == source,
                    InboundLead.external_id == parsed.external_id,
                )
            )
        ).scalars().first()
        if already is not None:
            row = InboundLead(
                business_id=business.id, source=source, external_id=parsed.external_id,
                name=parsed.name, phone=parsed.phone, email=parsed.email, query=parsed.query,
                status="duplicate", raw_payload=payload, received_at=now,
            )
            db.add(row)
            await db.flush()
            return row

    phone = None
    if parsed.phone:
        try:
            phone = normalise_phone(parsed.phone)
        except InvalidIdentifier:
            phone = None

    row = InboundLead(
        business_id=business.id, source=source, external_id=parsed.external_id,
        name=parsed.name, phone=phone, email=parsed.email, query=parsed.query,
        status="received" if phone else "no_phone", raw_payload=payload, received_at=now,
    )

    if phone:
        resolution = await resolver.resolve(
            business.id, IdentityKind.phone, phone, db, display_name=parsed.name,
        )
        row.customer_id = resolution.customer.id
        if parsed.query or parsed.email:
            label = PLATFORM_LABELS.get(source, source)
            parts = [f"{label} enquiry: {parsed.query or '-'}"]
            if parsed.email:
                parts.append(f"email: {parsed.email}")
            db.add(CustomerNote(
                business_id=business.id, customer_id=resolution.customer.id,
                body=" | ".join(parts)[:2000],
            ))

    db.add(row)
    await db.flush()
    return row
