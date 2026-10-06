"""
IndiaMART lead intake, the same two halves as Justdial:

- /indiamart/* (staff, JWT): generate the push URL, see recent leads.
- /webhooks/indiamart/{token} (public): the URL the business pastes into
  IndiaMART's Push API "Listener URL". The token is the only auth.

IndiaMART retries until it gets HTTP 200, and deactivates the integration if
nothing is accepted for 48 hours. So a valid token always gets 200, even for a
lead with no phone number. Only a wrong token gets 404.
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.config.settings import settings as app_settings
from shared.db.models import Business, InboundLead
from shared.leads import intake
from shared.leads.indiamart_parse import parse_indiamart

SOURCE = "indiamart"
TOKEN_SETTING = "indiamart_token_hash"

router = APIRouter(prefix="/indiamart", tags=["indiamart"])
public = APIRouter(prefix="/webhooks/indiamart", tags=["webhooks"])


class IndiamartSettingsOut(BaseModel):
    configured: bool
    webhook_url: str | None = None
    last_lead_at: datetime | None = None


class IndiamartLeadOut(BaseModel):
    id: str
    name: str | None
    phone: str | None
    email: str | None
    query: str | None
    status: str
    customer_id: str | None
    received_at: datetime


def _to_out(row: InboundLead) -> IndiamartLeadOut:
    return IndiamartLeadOut(
        id=str(row.id), name=row.name, phone=row.phone, email=row.email, query=row.query,
        status=row.status, customer_id=str(row.customer_id) if row.customer_id else None,
        received_at=row.received_at,
    )


@router.get("/settings", response_model=IndiamartSettingsOut)
async def get_indiamart_settings(current_user: CurrentUserDep, db: DbDep) -> IndiamartSettingsOut:
    business = await db.get(Business, current_user.business)
    configured = bool(business and (business.settings or {}).get(TOKEN_SETTING))
    last = await db.execute(
        select(func.max(InboundLead.received_at)).where(
            InboundLead.business_id == current_user.business,
            InboundLead.source == SOURCE,
        )
    )
    return IndiamartSettingsOut(configured=configured, last_lead_at=last.scalar_one())


@router.post("/token", response_model=IndiamartSettingsOut)
async def generate_indiamart_token(current_user: CurrentUserDep, db: DbDep) -> IndiamartSettingsOut:
    if not app_settings.public_base_url:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "PUBLIC_BASE_URL is not configured on the server"
        )
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")

    token = intake.new_token()
    business.settings = {**(business.settings or {}), TOKEN_SETTING: intake.hash_token(token)}
    await db.flush()
    base = app_settings.public_base_url.rstrip("/")
    return IndiamartSettingsOut(configured=True, webhook_url=f"{base}/webhooks/indiamart/{token}")


@router.get("/leads", response_model=list[IndiamartLeadOut])
async def list_indiamart_leads(current_user: CurrentUserDep, db: DbDep, limit: int = 20) -> list[IndiamartLeadOut]:
    rows = await db.execute(
        select(InboundLead)
        .where(InboundLead.business_id == current_user.business, InboundLead.source == SOURCE)
        .order_by(InboundLead.received_at.desc())
        .limit(min(max(limit, 1), 100))
    )
    return [_to_out(r) for r in rows.scalars().all()]


@public.post("/{token}")
async def receive_indiamart_lead(token: str, request: Request, db: DbDep) -> dict:
    found = (
        await db.execute(
            select(Business).where(
                Business.settings[TOKEN_SETTING].as_string() == intake.hash_token(token)
            )
        )
    ).scalars().first()
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Expected a JSON body")
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Expected a JSON object")

    row = await intake.ingest_parsed(db, found, SOURCE, parse_indiamart(payload), payload)
    return {"status": row.status, "id": str(row.id)}
