"""
Leads forwarded by email, received by our own Postfix (not a SaaS inbound
provider - see docs/email-leads-ec2-setup.md for the server side).

- /email-leads/* (staff, JWT): generate the forwarding address, see recent leads.
- /webhooks/email-leads (public): what Postfix's pipe script POSTs the raw
  email to. The address's local part ("leads-<token>") is the only auth - its
  hash is looked up the same way every other lead token is.

Always returns 200 (or close to it) for anything that parses as an email,
even an unknown token - the pipe script must never treat this as a bounce,
which would mail the portal back and risk a loop. An unknown token is logged
and dropped, not raised as an error.
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.config.settings import settings as app_settings
from shared.db.models import Business, InboundLead
from shared.care import post_call_actions
from shared.leads import intake
from shared.leads.email_parse import extract_lead_from_body, parse_raw_email, recipient_local_part
from shared.utils.logging import get_logger

logger = get_logger(__name__)

SOURCE = "email"
TOKEN_SETTING = "email_leads_token_hash"

router = APIRouter(prefix="/email-leads", tags=["email-leads"])
public = APIRouter(tags=["webhooks"])


class EmailLeadsSettingsOut(BaseModel):
    configured: bool
    address: str | None = None
    last_lead_at: datetime | None = None


@router.get("/settings", response_model=EmailLeadsSettingsOut)
async def get_email_leads_settings(current_user: CurrentUserDep, db: DbDep) -> EmailLeadsSettingsOut:
    business = await db.get(Business, current_user.business)
    configured = bool(business and (business.settings or {}).get(TOKEN_SETTING))
    last = await db.execute(
        select(func.max(InboundLead.received_at)).where(
            InboundLead.business_id == current_user.business,
            InboundLead.source == SOURCE,
        )
    )
    return EmailLeadsSettingsOut(configured=configured, last_lead_at=last.scalar_one())


@router.post("/token", response_model=EmailLeadsSettingsOut)
async def generate_email_leads_token(current_user: CurrentUserDep, db: DbDep) -> EmailLeadsSettingsOut:
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")

    token = intake.new_token()[:24]  # fits comfortably in an email local-part
    business.settings = {**(business.settings or {}), TOKEN_SETTING: intake.hash_token(token)}
    await db.flush()
    await post_call_actions.ensure_default_lead_automation(db, business.id)
    address = f"leads-{token}@{app_settings.email_leads_domain}"
    return EmailLeadsSettingsOut(configured=True, address=address)


class EmailLeadRowOut(BaseModel):
    id: str
    name: str | None
    phone: str | None
    email: str | None
    query: str | None
    status: str
    received_at: datetime


@router.get("/leads", response_model=list[EmailLeadRowOut])
async def list_email_leads(current_user: CurrentUserDep, db: DbDep, limit: int = 10) -> list[EmailLeadRowOut]:
    rows = await db.execute(
        select(InboundLead)
        .where(InboundLead.business_id == current_user.business, InboundLead.source == SOURCE)
        .order_by(InboundLead.received_at.desc())
        .limit(min(max(limit, 1), 100))
    )
    return [
        EmailLeadRowOut(
            id=str(r.id), name=r.name, phone=r.phone, email=r.email, query=r.query,
            status=r.status, received_at=r.received_at,
        )
        for r in rows.scalars().all()
    ]


@public.post("/webhooks/email-leads")
async def receive_email_lead(request: Request, db: DbDep) -> dict:
    raw = await request.body()
    try:
        email = parse_raw_email(raw)
    except Exception:
        logger.exception("could not parse an inbound lead email")
        return {"status": "ignored"}

    local_part = recipient_local_part(email.to_address)
    token = local_part[len("leads-"):] if local_part and local_part.startswith("leads-") else local_part
    if not token:
        logger.info("inbound lead email with no usable recipient: %r", email.to_address)
        return {"status": "ignored"}

    found = (
        await db.execute(
            select(Business).where(Business.settings[TOKEN_SETTING].as_string() == intake.hash_token(token))
        )
    ).scalars().first()
    if found is None:
        logger.info("inbound lead email for an unknown address: %s", local_part)
        return {"status": "ignored"}

    parsed = extract_lead_from_body(email.body_text)
    parsed.external_id = email.message_id
    payload = {
        "to": email.to_address, "from": email.from_address, "subject": email.subject,
        "body": email.body_text[:5000], "message_id": email.message_id,
    }
    row = await intake.ingest_parsed(db, found, SOURCE, parsed, payload)
    return {"status": row.status, "id": str(row.id)}
