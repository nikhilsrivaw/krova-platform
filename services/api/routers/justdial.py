"""
Justdial lead intake, two halves:

- /justdial/* (staff, JWT): generate the webhook URL, see recent leads.
- /webhooks/justdial/{token} (public): where Justdial's account manager
  points the business's leads. The token in the URL is the only auth, so the
  URL is treated as a secret. Generating a new one revokes the old one.

Mounted at the root for the webhook (same reasoning as webhooks.py: the URL
is registered with a third party and must not change later).
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select

from services.api.dependencies import OwnerOrAdminDep, CurrentUserDep, DbDep
from shared.config.settings import settings as app_settings
from shared.db.models import Business, InboundLead
from shared.care import post_call_actions
from shared.leads import justdial as justdial_leads

router = APIRouter(prefix="/justdial", tags=["justdial"])
public = APIRouter(prefix="/webhooks/justdial", tags=["webhooks"])


STEPS = (
    "Generate the URL below.",
    "Copy it, and send it to your Justdial account manager - ask them to set it as your lead webhook.",
    "Justdial's team activates it on their side, usually within a few days.",
    "Leads start showing up on the Leads page once it's live.",
)


class JustdialSettingsOut(BaseModel):
    configured: bool
    webhook_url: str | None = None
    last_lead_at: datetime | None = None
    steps: list[str] = list(STEPS)


class InboundLeadOut(BaseModel):
    id: str
    name: str | None
    phone: str | None
    email: str | None
    query: str | None
    status: str
    customer_id: str | None
    received_at: datetime


def _to_out(row: InboundLead) -> InboundLeadOut:
    return InboundLeadOut(
        id=str(row.id), name=row.name, phone=row.phone, email=row.email, query=row.query,
        status=row.status, customer_id=str(row.customer_id) if row.customer_id else None,
        received_at=row.received_at,
    )


@router.get("/settings", response_model=JustdialSettingsOut)
async def get_justdial_settings(current_user: CurrentUserDep, db: DbDep) -> JustdialSettingsOut:
    business = await db.get(Business, current_user.business)
    settings_map = (business.settings or {}) if business else {}
    configured = bool(settings_map.get(justdial_leads.TOKEN_SETTING))
    webhook_url = None
    enc = settings_map.get(justdial_leads.ENC_SETTING)
    if configured and enc and app_settings.public_base_url:
        token = justdial_leads.decrypt_token(enc)
        if token:
            webhook_url = f"{app_settings.public_base_url.rstrip('/')}/webhooks/justdial/{token}"
    last = await db.execute(
        select(func.max(InboundLead.received_at)).where(
            InboundLead.business_id == current_user.business,
            InboundLead.source == justdial_leads.SOURCE,
        )
    )
    return JustdialSettingsOut(configured=configured, webhook_url=webhook_url, last_lead_at=last.scalar_one())


@router.post("/token", response_model=JustdialSettingsOut)
async def generate_justdial_token(current_user: OwnerOrAdminDep, db: DbDep) -> JustdialSettingsOut:
    if not app_settings.public_base_url:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "PUBLIC_BASE_URL is not configured on the server"
        )
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")

    token = justdial_leads.new_token()
    business.settings = {
        **(business.settings or {}),
        justdial_leads.TOKEN_SETTING: justdial_leads.hash_token(token),
        justdial_leads.ENC_SETTING: justdial_leads.encrypt_token(token),
    }
    await db.flush()
    await post_call_actions.ensure_default_lead_automation(db, business.id)
    base = app_settings.public_base_url.rstrip("/")
    return JustdialSettingsOut(
        configured=True, webhook_url=f"{base}/webhooks/justdial/{token}",
    )


@router.get("/leads", response_model=list[InboundLeadOut])
async def list_justdial_leads(current_user: CurrentUserDep, db: DbDep, limit: int = 20) -> list[InboundLeadOut]:
    rows = await db.execute(
        select(InboundLead)
        .where(InboundLead.business_id == current_user.business, InboundLead.source == justdial_leads.SOURCE)
        .order_by(InboundLead.received_at.desc())
        .limit(min(max(limit, 1), 100))
    )
    return [_to_out(r) for r in rows.scalars().all()]


@public.post("/{token}")
async def receive_justdial_lead(token: str, request: Request, db: DbDep) -> dict:
    found = (
        await db.execute(
            select(Business).where(
                Business.settings[justdial_leads.TOKEN_SETTING].as_string()
                == justdial_leads.hash_token(token)
            )
        )
    ).scalars().first()
    if found is None:
        # Same answer for "no such token" as for anything else wrong with the URL.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Expected a JSON body")
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Expected a JSON object")

    row = await justdial_leads.ingest(db, found, payload)
    return {"status": row.status, "id": str(row.id)}
