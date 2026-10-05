"""
Justdial lead intake: one lead in, one InboundLead row out, and the customer
behind the phone number gets the enquiry as a CRM note.

Dedupe is by Justdial's own lead id, when the payload has one. Without an id
the same lead sent twice would be stored twice, and that is the known gap.

What this does NOT do: send a WhatsApp message to the lead. A lead has never
messaged the business, so the first message has to be an approved template,
and no Justdial template exists yet.
"""

import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Business, CustomerNote, IdentityKind, InboundLead
from shared.identity import resolver
from shared.identity.normalise import InvalidIdentifier, normalise_phone
from shared.leads.justdial_parse import parse_lead

SOURCE = "justdial"
TOKEN_SETTING = "justdial_token_hash"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    # Only the hash is stored. A lost URL means generating a new one.
    return hashlib.sha256(token.encode()).hexdigest()


async def ingest(db: AsyncSession, business: Business, payload: dict) -> InboundLead:
    parsed = parse_lead(payload)
    now = datetime.now(timezone.utc)

    if parsed.external_id:
        already = (
            await db.execute(
                select(InboundLead).where(
                    InboundLead.business_id == business.id,
                    InboundLead.source == SOURCE,
                    InboundLead.external_id == parsed.external_id,
                )
            )
        ).scalars().first()
        if already is not None:
            row = InboundLead(
                business_id=business.id, source=SOURCE, external_id=parsed.external_id,
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
        business_id=business.id, source=SOURCE, external_id=parsed.external_id,
        name=parsed.name, phone=phone, email=parsed.email, query=parsed.query,
        status="received" if phone else "no_phone", raw_payload=payload, received_at=now,
    )

    if phone:
        resolution = await resolver.resolve(
            business.id, IdentityKind.phone, phone, db, display_name=parsed.name,
        )
        row.customer_id = resolution.customer.id
        if parsed.query or parsed.email:
            parts = [f"Justdial enquiry: {parsed.query or '-'}"]
            if parsed.email:
                parts.append(f"email: {parsed.email}")
            db.add(CustomerNote(
                business_id=business.id, customer_id=resolution.customer.id,
                body=" | ".join(parts)[:2000],
            ))

    db.add(row)
    await db.flush()
    return row
