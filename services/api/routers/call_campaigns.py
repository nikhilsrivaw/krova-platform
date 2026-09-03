"""
Outbound call campaigns - the voice-channel counterpart to campaigns.py's
WhatsApp broadcasts.

Kept as its own router rather than folded into campaigns.py: that file
imports WhatsAppClient throughout and is built entirely around templates,
none of which apply to a phone call. What genuinely is shared - the
Audience enum and campaigns/audience.py's resolve() - is reused directly,
unchanged, rather than copied.

The one deliberate structural difference from campaigns.py's send_campaign:
this enqueues one job per recipient onto the Postgres job queue
(call_campaign_dial, see services/workers/call_campaign.py) instead of
looping through recipients synchronously inside the request. A WhatsApp
send is a sub-second API round-trip; a phone call can take a minute or
more, and holding an HTTP request open per recipient (or for a whole
campaign) is not the same latency shape at all.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.campaigns import audience as audience_module
from shared.db import queue
from shared.db.models import (
    Audience,
    CallCampaign,
    CallCampaignRecipient,
    CallCampaignRecipientStatus,
    CallCampaignStatus,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/call-campaigns", tags=["call-campaigns"])


# ── audiences (thin wrap - same Audience enum campaigns.py already uses) ────

_AUDIENCE_LABELS: dict[Audience, str] = {
    Audience.owes_money: "Owes money",
    Audience.overdue: "Overdue",
    Audience.we_promised: "We promised them something",
    Audience.gone_quiet: "Gone quiet",
    Audience.by_tag: "By tag",
    Audience.all_customers: "All customers",
}
_NEEDS_PARAMS = {Audience.gone_quiet, Audience.by_tag}


class AudienceOut(BaseModel):
    value: str
    label: str
    needs_params: bool


@router.get("/audiences", response_model=list[AudienceOut])
async def list_audiences() -> list[AudienceOut]:
    return [
        AudienceOut(value=a.value, label=_AUDIENCE_LABELS[a], needs_params=a in _NEEDS_PARAMS)
        for a in Audience
    ]


# ── preview ──────────────────────────────────────────────────────────────

class CallCampaignIn(BaseModel):
    name: str
    audience: str
    audience_params: dict = {}
    objective: str


class RecipientPreviewOut(BaseModel):
    customer_id: str
    name: str | None
    phone_masked: str


class CallCampaignPreviewOut(BaseModel):
    audience: str
    audience_label: str
    will_reach: int
    will_skip: int
    skipped_reasons: list[dict]
    sample: list[RecipientPreviewOut]


def _mask(phone: str) -> str:
    return f"{phone[:4]}••••{phone[-2:]}" if len(phone) > 6 else "••••"


@router.post("/preview", response_model=CallCampaignPreviewOut)
async def preview(body: CallCampaignIn, current_user: CurrentUserDep, db: DbDep) -> CallCampaignPreviewOut:
    try:
        audience = Audience(body.audience)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown audience")

    result = await audience_module.resolve(current_user.business, audience, body.audience_params, db)

    return CallCampaignPreviewOut(
        audience=audience.value,
        audience_label=_AUDIENCE_LABELS[audience],
        will_reach=result.count,
        will_skip=len(result.skipped),
        skipped_reasons=result.skipped[:20],
        sample=[
            RecipientPreviewOut(customer_id=str(r.customer_id), name=r.name, phone_masked=_mask(r.phone))
            for r in result.recipients[:5]
        ],
    )


# ── CRUD ─────────────────────────────────────────────────────────────────

class CallCampaignOut(BaseModel):
    id: str
    name: str
    audience: str
    audience_label: str
    objective: str
    status: str
    recipients: int
    sent_count: int
    failed_count: int
    skipped_count: int
    created_at: str
    completed_at: str | None


def _out(campaign: CallCampaign) -> CallCampaignOut:
    audience = campaign.audience if isinstance(campaign.audience, Audience) else Audience(campaign.audience)
    return CallCampaignOut(
        id=str(campaign.id),
        name=campaign.name,
        audience=audience.value,
        audience_label=_AUDIENCE_LABELS[audience],
        objective=campaign.objective,
        status=campaign.status.value if hasattr(campaign.status, "value") else campaign.status,
        recipients=campaign.recipients,
        sent_count=campaign.sent_count,
        failed_count=campaign.failed_count,
        skipped_count=campaign.skipped_count,
        created_at=campaign.created_at.isoformat(),
        completed_at=campaign.completed_at.isoformat() if campaign.completed_at else None,
    )


@router.post("", response_model=CallCampaignOut, status_code=status.HTTP_201_CREATED)
async def create_call_campaign(body: CallCampaignIn, current_user: CurrentUserDep, db: DbDep) -> CallCampaignOut:
    try:
        audience = Audience(body.audience)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown audience")

    if not body.objective.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="objective is required")

    campaign = CallCampaign(
        business_id=current_user.business,
        name=body.name.strip(),
        audience=audience,
        audience_params=body.audience_params,
        objective=body.objective.strip(),
        status=CallCampaignStatus.draft,
        created_by_user_id=current_user.id,
    )
    db.add(campaign)
    await db.commit()
    return _out(campaign)


@router.get("", response_model=list[CallCampaignOut])
async def list_call_campaigns(current_user: CurrentUserDep, db: DbDep) -> list[CallCampaignOut]:
    rows = (
        await db.execute(
            select(CallCampaign)
            .where(CallCampaign.business_id == current_user.business)
            .order_by(CallCampaign.created_at.desc())
        )
    ).scalars().all()
    return [_out(c) for c in rows]


@router.post("/{campaign_id}/send", response_model=CallCampaignOut)
async def send_call_campaign(campaign_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> CallCampaignOut:
    """
    Resolve the audience once, write one CallCampaignRecipient per person,
    enqueue one call_campaign_dial job per recipient, and return
    immediately - the worker places the actual calls. Never loops through
    recipients inline; see this module's own docstring for why that would
    be wrong for voice specifically.
    """
    campaign = await db.get(CallCampaign, campaign_id)
    if campaign is None or campaign.business_id != current_user.business:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call campaign not found")
    if campaign.status != CallCampaignStatus.draft:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Campaign is not in draft (status: {campaign.status.value})",
        )

    audience = campaign.audience if isinstance(campaign.audience, Audience) else Audience(campaign.audience)
    result = await audience_module.resolve(current_user.business, audience, campaign.audience_params, db)

    if not result.recipients:
        campaign.status = CallCampaignStatus.failed
        campaign.last_error = "No customers matched this audience"
        await db.commit()
        return _out(campaign)

    for recipient in result.recipients:
        row = CallCampaignRecipient(
            call_campaign_id=campaign.id,
            customer_id=recipient.customer_id,
            status=CallCampaignRecipientStatus.pending,
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        await db.flush()
        await queue.enqueue(
            "call_campaign_dial", {"recipient_id": str(row.id)}, db
        )

    campaign.recipients = len(result.recipients)
    campaign.sent_count = len(result.recipients)  # jobs enqueued - see CallCampaign.sent_count's own docstring
    campaign.skipped_count = len(result.skipped)
    campaign.status = CallCampaignStatus.sending
    campaign.started_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info(
        "call campaign send business=%s campaign=%s recipients=%s",
        current_user.business, campaign.id, len(result.recipients),
    )
    return _out(campaign)
