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

import re
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.dependencies import OwnerOrAdminDep, CurrentUserDep, DbDep
from shared.campaigns import audience as audience_module
from shared.db import queue
from shared.db.models import (
    Audience,
    Business,
    CallCampaign,
    CallCampaignRecipient,
    CallCampaignRecipientStatus,
    CallCampaignStatus,
    CallScript,
    Channel,
    ChannelConnection,
    ConnectionStatus,
)
from shared.audit import activity
from shared.channels.voice import numbers as voice_numbers
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
    Audience.numbers: "Specific phone numbers",
}
_NEEDS_PARAMS = {Audience.gone_quiet, Audience.by_tag, Audience.numbers}


class AudienceOut(BaseModel):
    value: str
    label: str
    needs_params: bool


@router.get("/audiences", response_model=list[AudienceOut])
async def list_audiences(current_user: CurrentUserDep) -> list[AudienceOut]:
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
    # Set to run a fixed CallScript instead of an improvised, objective-
    # only call - see CallCampaign.call_script_id's own docstring. `objective`
    # stays required either way, as the campaign's own descriptive label.
    call_script_id: str | None = None
    # "service" or "promotional". Decides which number series may place the calls.
    purpose: Literal["service", "promotional"] | None = None
    # The voice number to call from. None = choose automatically (the one whose series suits the purpose).
    connection_id: str | None = None


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

    # create_missing=False: a preview re-runs as someone types, so it must
    # not create a customer for a half-entered number.
    result = await audience_module.resolve(
        current_user.business, audience, body.audience_params, db, create_missing=False
    )

    return CallCampaignPreviewOut(
        audience=audience.value,
        audience_label=_AUDIENCE_LABELS[audience],
        will_reach=result.count,
        will_skip=len(result.skipped),
        skipped_reasons=result.skipped[:20],
        sample=[
            RecipientPreviewOut(
                customer_id=str(r.customer_id) if r.customer_id else "new",
                name=r.name,
                phone_masked=_mask(r.phone),
            )
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
    purpose: str | None
    connection_id: str | None = None
    call_script_id: str | None
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
        purpose=campaign.purpose,
        connection_id=str(campaign.connection_id) if campaign.connection_id else None,
        call_script_id=str(campaign.call_script_id) if campaign.call_script_id else None,
        status=campaign.status.value if hasattr(campaign.status, "value") else campaign.status,
        recipients=campaign.recipients,
        sent_count=campaign.sent_count,
        failed_count=campaign.failed_count,
        skipped_count=campaign.skipped_count,
        created_at=campaign.created_at.isoformat(),
        completed_at=campaign.completed_at.isoformat() if campaign.completed_at else None,
    )


@router.post("", response_model=CallCampaignOut, status_code=status.HTTP_201_CREATED)
async def create_call_campaign(body: CallCampaignIn, current_user: OwnerOrAdminDep, db: DbDep) -> CallCampaignOut:
    try:
        audience = Audience(body.audience)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown audience")

    if not body.objective.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="objective is required")

    call_script_uuid = None
    if body.call_script_id:
        call_script_uuid = uuid.UUID(body.call_script_id)
        script = await db.get(CallScript, call_script_uuid)
        if script is None or script.business_id != current_user.business:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call script not found")

    connection_uuid = None
    if body.connection_id:
        try:
            connection_uuid = uuid.UUID(body.connection_id)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown number")
        chosen = next(
            (c for c in await voice_numbers.active(db, current_user.business) if c.id == connection_uuid), None,
        )
        if chosen is None or not voice_numbers.allows_outbound(chosen):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="That number is not connected here or only takes incoming calls.",
            )

    campaign = CallCampaign(
        business_id=current_user.business,
        connection_id=connection_uuid,
        name=body.name.strip(),
        audience=audience,
        audience_params=body.audience_params,
        objective=body.objective.strip(),
        purpose=body.purpose,
        status=CallCampaignStatus.draft,
        created_by_user_id=current_user.id,
        call_script_id=call_script_uuid,
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


# Kept under their old names for anything that still imports them.
_SERIES_FOR_PURPOSE = voice_numbers.SERIES_FOR_PURPOSE
_series_of = voice_numbers.series_of


async def _check_number_series(campaign: CallCampaign, business: Business | None, db: AsyncSession) -> None:
    """
    India's rules: service and transactional calls go on 080 or 022, promotional calls on 140,
    and BFSI-only calls on 160. A campaign is refused before any call is placed if its purpose
    does not match the number it would actually dial from.

    The number is the one the campaign names ("call from"), or - if it names none - the one whose
    series suits its purpose, then the business's default for outgoing calls. The series is read
    from that number's own digits, so a business with a 140 line and an 080 line is judged on the
    line it is really using, not on whichever happened to come first.
    """
    if campaign.purpose not in voice_numbers.SERIES_FOR_PURPOSE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Choose whether this campaign is a service call or a promotional call first.",
        )
    allowed = voice_numbers.SERIES_FOR_PURPOSE[campaign.purpose]

    numbers = await voice_numbers.active(db, campaign.business_id)
    connection = voice_numbers.choose_outbound(numbers, purpose=campaign.purpose, preferred=campaign.connection_id)
    if connection is None:
        if campaign.connection_id is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The number chosen for this campaign is no longer connected or only takes incoming calls. Choose another.",
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No voice number is connected that can place calls, so there is nothing to call from.",
        )
    number = connection.external_account_id
    actual = voice_numbers.series_of(number)
    name = voice_numbers.label_of(connection)
    if actual is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The number {name} ({number}) is not an 080, 022, 140 or 160 series number.",
        )
    if actual not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A {campaign.purpose} campaign needs a {' or '.join(sorted(allowed))} number, "
                f"but {name} ({number}) is in the {actual} series. "
                "Choose a number in the right series under 'Call from', or buy one."
            ),
        )


@router.post("/{campaign_id}/send", response_model=CallCampaignOut)
async def send_call_campaign(campaign_id: uuid.UUID, current_user: OwnerOrAdminDep, db: DbDep) -> CallCampaignOut:
    """
    Resolve the audience once, write one CallCampaignRecipient per person,
    enqueue one call_campaign_dial job per recipient, and return
    immediately - the worker places the actual calls. Never loops through
    recipients inline; see this module's own docstring for why that would
    be wrong for voice specifically.
    """
    from shared.billing.guards import require_active_plan, require_wallet

    await require_active_plan(db, current_user.business)
    await require_wallet(db, current_user.business)
    campaign = await db.get(CallCampaign, campaign_id)
    if campaign is None or campaign.business_id != current_user.business:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call campaign not found")
    if campaign.status != CallCampaignStatus.draft:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Campaign is not in draft (status: {campaign.status.value})",
        )
    await _check_number_series(campaign, await db.get(Business, current_user.business), db)

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

    activity.note(recipients=len(result.recipients), skipped=len(result.skipped), purpose=campaign.purpose)
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
