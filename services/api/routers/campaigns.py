"""
Campaigns.

The preview is the important endpoint here, not the send. A business about to
message forty people should see who they are, what it will cost, which
category Meta will bill it as, and how many will not receive it — before
anything leaves.

Blasting first and reporting after is how numbers get their quality rating
destroyed. Quality falls, Meta lowers the sending limit, and a business that
was reaching 250 people a day is suddenly reaching 50 with no idea why.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.auth.encryption import decrypt
from shared.campaigns import audience as audience_module
from shared.channels import ingest
from shared.channels.whatsapp.client import CarouselSendCard, WhatsAppClient, WhatsAppError
from shared.db.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    Channel,
    ChannelConnection,
    ConnectionStatus,
    Direction,
    IdentityKind,
    MessageTemplate,
    TemplateStatus,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

# 4 messages/second - comfortably under Meta's 80/s hard throughput limit,
# while still visibly paced rather than bursted. See send_campaign's own
# comment at the call site for why pacing matters even under the hard limit.
SEND_PACE_SECONDS = 0.25

AUDIENCE_LABELS = {
    Audience.owes_money: "Everyone who owes you money",
    Audience.overdue: "Everyone whose payment is overdue",
    Audience.we_promised: "Everyone you promised something to",
    Audience.gone_quiet: "Everyone who has gone quiet",
    Audience.by_tag: "Everyone with a chosen CRM tag",
    Audience.all_customers: "Every customer",
}


def _audience_label(audience: Audience, params: dict) -> str:
    if audience == Audience.by_tag:
        tag = (params or {}).get("tag")
        return f"Tagged '{tag}'" if tag else AUDIENCE_LABELS[Audience.by_tag]
    return AUDIENCE_LABELS.get(audience, _value(audience))


class CampaignCardIn(BaseModel):
    media_id: str = Field(min_length=1)
    variable_mapping: list[str] = Field(default_factory=list)


class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    audience: Literal[
        "owes_money", "overdue", "we_promised", "gone_quiet", "by_tag", "all_customers"
    ]
    audience_params: dict = Field(default_factory=dict)
    template_name: str
    template_language: str = "en"
    # Which of the recipient's own values fill the template's variables, in
    # order: ["customer_name", "amount", "due_date"]
    variable_mapping: list[str] = Field(default_factory=list)
    # Present only when template_name is a carousel template - one entry per
    # card, same order the template was approved with.
    carousel_cards: list[CampaignCardIn] = Field(default_factory=list)


class RecipientPreview(BaseModel):
    customer_id: str
    name: str | None
    phone_masked: str
    message_preview: str


class CampaignPreview(BaseModel):
    audience: str
    audience_label: str
    will_reach: int
    will_skip: int
    skipped_reasons: list[dict]
    total_outstanding: str | None
    template: str
    template_status: str
    category: str | None
    cost_note: str
    daily_limit_note: str | None
    sample: list[RecipientPreview]


class CampaignOut(BaseModel):
    id: str
    name: str
    audience: str
    audience_label: str
    status: str
    template_name: str | None
    category: str | None
    recipients: int
    sent_count: int
    failed_count: int
    skipped_count: int
    created_at: str
    completed_at: str | None


def _value(v) -> str:
    return v.value if hasattr(v, "value") else str(v)


def _mask(phone: str) -> str:
    return f"{phone[:5]}…{phone[-3:]}" if len(phone) > 8 else phone


def _fill(template_body: str, values: dict, mapping: list[str]) -> str:
    """Render what this recipient will actually read."""
    text = template_body or ""
    for index, key in enumerate(mapping, start=1):
        text = text.replace(f"{{{{{index}}}}}", values.get(key, ""))
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return text


async def _connection(business_id: uuid.UUID, db: DbDep) -> ChannelConnection:
    result = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == business_id,
            ChannelConnection.channel == Channel.whatsapp,
            ChannelConnection.status == ConnectionStatus.active,
        )
    )
    connection = result.scalars().first()
    if connection is None or not connection.access_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Connect WhatsApp first"
        )
    return connection


async def _template(
    business_id: uuid.UUID, name: str, language: str, db: DbDep
) -> MessageTemplate:
    result = await db.execute(
        select(MessageTemplate).where(
            MessageTemplate.business_id == business_id,
            MessageTemplate.name == name,
            MessageTemplate.language == language,
        )
    )
    template = result.scalars().first()
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No template called '{name}' in {language}",
        )
    return template


@router.get("/audiences")
async def list_audiences(current_user: CurrentUserDep) -> list[dict]:
    """The questions a campaign can ask of the ledger."""
    return [
        {
            "value": a.value,
            "label": AUDIENCE_LABELS[a],
            "needs_params": a in (Audience.gone_quiet, Audience.by_tag),
        }
        for a in Audience
    ]


@router.post("/preview", response_model=CampaignPreview)
async def preview(
    body: CampaignIn, current_user: CurrentUserDep, db: DbDep
) -> CampaignPreview:
    """
    Who this reaches, what it costs, and who it misses — before sending.

    The endpoint that stops a business destroying its own quality rating.
    """
    business_id = current_user.business
    template = await _template(business_id, body.template_name, body.template_language, db)

    result = await audience_module.resolve(
        business_id, Audience(body.audience), body.audience_params, db
    )

    category = _value(template.category)
    if category == "MARKETING":
        cost_note = (
            "Marketing templates are always charged by Meta, and are the most "
            "likely to be marked as spam. If this is a payment reminder or a "
            "confirmation, a utility template costs less and lands better."
        )
    elif category == "UTILITY":
        cost_note = (
            "Utility templates are free inside the 24-hour window and cheap "
            "outside it. This is the right category for reminders."
        )
    else:
        cost_note = "Authentication templates are for verification codes only."

    limit_note = None
    used = await audience_module.sent_today(business_id, db)
    tier = (await _connection(business_id, db)).extra or {}
    daily = {"TIER_250": 250, "TIER_1K": 1000, "TIER_10K": 10000}.get(
        tier.get("messaging_limit_tier", ""), None
    )
    if daily and used + result.count > daily:
        limit_note = (
            f"You have messaged {used} people today and your limit is {daily}. "
            f"About {max(0, daily - used)} of these {result.count} will send "
            "today; the rest will go out tomorrow."
        )

    sample = [
        RecipientPreview(
            customer_id=str(r.customer_id),
            name=r.name,
            phone_masked=_mask(r.phone),
            message_preview=_fill(template.body_text or "", r.values, body.variable_mapping),
        )
        for r in result.recipients[:5]
    ]

    return CampaignPreview(
        audience=body.audience,
        audience_label=_audience_label(Audience(body.audience), body.audience_params),
        will_reach=result.count,
        will_skip=len(result.skipped),
        skipped_reasons=result.skipped[:10],
        total_outstanding=(
            f"₹{result.total_amount_paise / 100:,.0f}"
            if result.total_amount_paise
            else None
        ),
        template=template.name,
        template_status=_value(template.status),
        category=category,
        cost_note=cost_note,
        daily_limit_note=limit_note,
        sample=sample,
    )


@router.post("", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignIn, current_user: CurrentUserDep, db: DbDep
) -> CampaignOut:
    """Save a campaign. Nothing sends until it is started."""
    template = await _template(
        current_user.business, body.template_name, body.template_language, db
    )
    if template.status != TemplateStatus.approved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"That template is {_value(template.status).lower()}. Only "
                "approved templates can be sent."
            ),
        )

    campaign = Campaign(
        business_id=current_user.business,
        name=body.name.strip(),
        audience=Audience(body.audience),
        audience_params=body.audience_params,
        template_name=body.template_name,
        template_language=body.template_language,
        variable_mapping=body.variable_mapping,
        carousel_cards=[
            {"media_id": c.media_id, "variable_mapping": c.variable_mapping}
            for c in body.carousel_cards
        ],
        category=_value(template.category),
        created_by_user_id=current_user.id,
        status=CampaignStatus.draft,
    )
    db.add(campaign)
    await db.flush()
    return _out(campaign)


@router.post("/{campaign_id}/send", response_model=CampaignOut)
async def send_campaign(
    campaign_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep
) -> CampaignOut:
    """
    Send it.

    Stops cleanly at the daily limit rather than burning through it: the
    remainder is left pending and the campaign pauses, so tomorrow it
    continues instead of the last hundred people silently failing.
    """
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or campaign.business_id != current_user.business:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status not in (CampaignStatus.draft, CampaignStatus.paused):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This campaign is already {_value(campaign.status)}",
        )

    connection = await _connection(current_user.business, db)
    template = await _template(
        current_user.business, campaign.template_name or "", campaign.template_language, db
    )

    result = await audience_module.resolve(
        current_user.business, campaign.audience, campaign.audience_params, db
    )

    used = await audience_module.sent_today(current_user.business, db)
    daily = {"TIER_250": 250, "TIER_1K": 1000, "TIER_10K": 10000}.get(
        (connection.extra or {}).get("messaging_limit_tier", ""), 250
    )
    remaining = max(0, daily - used)

    now = datetime.now(timezone.utc)
    campaign.status = CampaignStatus.sending
    campaign.started_at = now
    campaign.recipients = result.count
    campaign.skipped_count = len(result.skipped)

    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)

    sent = failed = 0
    for recipient in result.recipients:
        if sent >= remaining:
            db.add(
                CampaignRecipient(
                    campaign_id=campaign.id,
                    customer_id=recipient.customer_id,
                    status="pending",
                    reason="Daily limit reached - will send tomorrow",
                    created_at=now,
                )
            )
            continue

        variables = [recipient.values.get(k, "") for k in campaign.variable_mapping]
        carousel_cards = [
            CarouselSendCard(
                media_id=card["media_id"],
                body_params=[recipient.values.get(k, "") for k in card.get("variable_mapping", [])],
            )
            for card in campaign.carousel_cards
        ]

        # Paced, not bursted. Meta's hard throughput limit (80 msg/s default)
        # is not the risk for a business-sized campaign - quality rating is:
        # a tight burst of business-initiated messages reads as spam-like to
        # Meta's algorithms even well under the daily tier cap, the same
        # reasoning this module's own docstring warns about. Every BSP
        # (Wati, AiSensy, Gupshup) paces broadcast sends for this reason,
        # even though Meta publishes no exact recommended rate.
        await asyncio.sleep(SEND_PACE_SECONDS)

        try:
            outcome = await client.send_template(
                recipient.phone,
                campaign.template_name,
                campaign.template_language,
                body_params=variables or None,
                carousel_cards=carousel_cards or None,
            )
        except WhatsAppError as exc:
            failed += 1
            db.add(
                CampaignRecipient(
                    campaign_id=campaign.id,
                    customer_id=recipient.customer_id,
                    status="failed",
                    reason=str(exc),
                    variables=variables,
                    created_at=now,
                )
            )
            continue

        stored = await ingest.ingest(
            business_id=current_user.business,
            channel=Channel.whatsapp,
            direction=Direction.outbound,
            identity_kind=IdentityKind.phone,
            identity_value=recipient.phone,
            external_id=outcome.external_id,
            text=_fill(template.body_text or "", recipient.values, campaign.variable_mapping),
            occurred_at=datetime.now(timezone.utc),
            connection_id=connection.id,
            raw={"campaign_id": str(campaign.id), "template": campaign.template_name},
            enqueue_analysis=False,
            db=db,
        )
        sent += 1
        db.add(
            CampaignRecipient(
                campaign_id=campaign.id,
                customer_id=recipient.customer_id,
                status="sent",
                variables=variables,
                message_id=stored.message.id if stored.message else None,
                sent_at=datetime.now(timezone.utc),
                created_at=now,
            )
        )

    campaign.sent_count = sent
    campaign.failed_count = failed
    held_back = result.count - sent - failed
    campaign.status = CampaignStatus.paused if held_back > 0 else CampaignStatus.sent
    campaign.completed_at = None if held_back > 0 else datetime.now(timezone.utc)

    logger.info(
        "campaign %s: %s sent, %s failed, %s held for tomorrow",
        campaign.id, sent, failed, held_back,
    )
    return _out(campaign)


@router.get("", response_model=list[CampaignOut])
async def list_campaigns(current_user: CurrentUserDep, db: DbDep) -> list[CampaignOut]:
    result = await db.execute(
        select(Campaign)
        .where(Campaign.business_id == current_user.business)
        .order_by(Campaign.created_at.desc())
    )
    return [_out(c) for c in result.scalars().all()]


def _out(c: Campaign) -> CampaignOut:
    return CampaignOut(
        id=str(c.id),
        name=c.name,
        audience=_value(c.audience),
        audience_label=_audience_label(c.audience, c.audience_params),
        status=_value(c.status),
        template_name=c.template_name,
        category=c.category,
        recipients=c.recipients,
        sent_count=c.sent_count,
        failed_count=c.failed_count,
        skipped_count=c.skipped_count,
        created_at=c.created_at.isoformat(),
        completed_at=c.completed_at.isoformat() if c.completed_at else None,
    )
