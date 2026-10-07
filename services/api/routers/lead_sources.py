"""
Lead sources that use the shared generic intake: Magicbricks, 99Acres,
Housing.com, and any other tool that can POST JSON (see shared/leads/platforms.py).

- /lead-sources/* (staff, JWT): list the sources with their steps, generate a
  URL per source, and see recent leads from all of them.
- /webhooks/leads/{platform}/{token} (public): the URL a platform or tool posts
  to. The token is the only auth, and only its hash is stored.
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
from shared.leads.justdial_parse import parse_lead
from shared.leads.platforms import BY_KEY, PLATFORMS

router = APIRouter(prefix="/lead-sources", tags=["lead-sources"])
public = APIRouter(prefix="/webhooks/leads", tags=["webhooks"])


class LeadSourceOut(BaseModel):
    key: str
    label: str
    setup: str
    steps: list[str]
    configured: bool
    webhook_url: str | None = None
    last_lead_at: datetime | None = None


class LeadSourceTokenOut(BaseModel):
    key: str
    webhook_url: str


class LeadRowOut(BaseModel):
    id: str
    source: str
    name: str | None
    phone: str | None
    email: str | None
    query: str | None
    status: str
    received_at: datetime


@router.get("", response_model=list[LeadSourceOut])
async def list_lead_sources(current_user: CurrentUserDep, db: DbDep) -> list[LeadSourceOut]:
    business = await db.get(Business, current_user.business)
    settings_map = (business.settings or {}) if business else {}
    out = []
    for platform in PLATFORMS:
        last = await db.execute(
            select(func.max(InboundLead.received_at)).where(
                InboundLead.business_id == current_user.business,
                InboundLead.source == platform.key,
            )
        )
        configured = bool(settings_map.get(platform.token_setting))
        webhook_url = None
        enc = settings_map.get(platform.enc_setting)
        if configured and enc and app_settings.public_base_url:
            token = intake.decrypt_token(enc)
            if token:
                webhook_url = f"{app_settings.public_base_url.rstrip('/')}/webhooks/leads/{platform.key}/{token}"
        out.append(LeadSourceOut(
            key=platform.key,
            label=platform.label,
            setup=platform.setup,
            steps=list(platform.steps),
            configured=configured,
            webhook_url=webhook_url,
            last_lead_at=last.scalar_one(),
        ))
    return out


@router.post("/{key}/token", response_model=LeadSourceTokenOut)
async def generate_lead_source_token(key: str, current_user: CurrentUserDep, db: DbDep) -> LeadSourceTokenOut:
    platform = BY_KEY.get(key)
    if platform is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown lead source")
    if not app_settings.public_base_url:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "PUBLIC_BASE_URL is not configured on the server")
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")

    token = intake.new_token()
    business.settings = {
        **(business.settings or {}),
        platform.token_setting: intake.hash_token(token),
        platform.enc_setting: intake.encrypt_token(token),
    }
    await db.flush()
    await post_call_actions.ensure_default_lead_automation(db, business.id)
    base = app_settings.public_base_url.rstrip("/")
    return LeadSourceTokenOut(key=key, webhook_url=f"{base}/webhooks/leads/{key}/{token}")


@router.get("/leads", response_model=list[LeadRowOut])
async def list_lead_source_leads(current_user: CurrentUserDep, db: DbDep, limit: int = 20) -> list[LeadRowOut]:
    keys = [p.key for p in PLATFORMS]
    rows = await db.execute(
        select(InboundLead)
        .where(InboundLead.business_id == current_user.business, InboundLead.source.in_(keys))
        .order_by(InboundLead.received_at.desc())
        .limit(min(max(limit, 1), 100))
    )
    return [
        LeadRowOut(
            id=str(r.id), source=r.source, name=r.name, phone=r.phone, email=r.email,
            query=r.query, status=r.status, received_at=r.received_at,
        )
        for r in rows.scalars().all()
    ]


@public.post("/{key}/{token}")
async def receive_lead_source(key: str, token: str, request: Request, db: DbDep) -> dict:
    platform = BY_KEY.get(key)
    if platform is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    found = (
        await db.execute(
            select(Business).where(
                Business.settings[platform.token_setting].as_string() == intake.hash_token(token)
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

    row = await intake.ingest_parsed(db, found, key, parse_lead(payload), payload)
    return {"status": row.status, "id": str(row.id)}
