"""
Drip step execution - the follow-up after a campaign's own first send.

A campaign step is not a second campaign and not a general workflow engine.
It reuses the campaign's own audience QUESTION (shared/campaigns/audience.py)
rather than a frozen recipient list, so someone who has since paid, replied,
or opted out drops out of the sequence automatically - exactly as they would
from a second manual campaign against the same audience. There is no
authored condition language: a step's own "condition" is one of two real,
already-tracked signals ("always" or "no_reply"), the same discipline
Audience already applies to who a campaign reaches in the first place.

Runs as a periodic sweep (services/api/scheduler.py), the same shape as
every other "scan due work, act once" job in this codebase
(shared/scheduling/recall.py, shared/care/cod_call_failsafe.py) - not a new
execution engine, not a job queue, not a state machine library.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt
from shared.campaigns import audience as audience_module
from shared.channels import ingest
from shared.channels.whatsapp.client import CarouselSendCard, WhatsAppClient, WhatsAppError
from shared.db.models import (
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    CampaignStep,
    CampaignStepRecipient,
    Channel,
    ChannelConnection,
    ConnectionStatus,
    Direction,
    FlowSendLog,
    IdentityKind,
    Message,
    MessageTemplate,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Same pacing discipline as campaigns.py's own send_campaign, and for the
# identical reason - a tight burst of business-initiated messages reads as
# spam-like to Meta's own algorithms well under the daily tier cap.
SEND_PACE_SECONDS = 0.25

TIER_DAILY_LIMITS = {"TIER_250": 250, "TIER_1K": 1000, "TIER_10K": 10000}


def _value(v) -> str:
    return v.value if hasattr(v, "value") else str(v)


def _fill(template_body: str, values: dict, mapping: list[str]) -> str:
    text = template_body or ""
    for index, key in enumerate(mapping, start=1):
        text = text.replace(f"{{{{{index}}}}}", values.get(key, ""))
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return text


async def _active_connection(business_id: uuid.UUID, db: AsyncSession) -> ChannelConnection | None:
    result = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == business_id,
            ChannelConnection.channel == Channel.whatsapp,
            ChannelConnection.status == ConnectionStatus.active,
        )
    )
    connection = result.scalars().first()
    if connection is None or not connection.access_token:
        return None
    return connection


async def _template(business_id: uuid.UUID, name: str, language: str, db: AsyncSession) -> MessageTemplate | None:
    result = await db.execute(
        select(MessageTemplate).where(
            MessageTemplate.business_id == business_id,
            MessageTemplate.name == name,
            MessageTemplate.language == language,
        )
    )
    return result.scalars().first()


async def _replied_since(customer_id: uuid.UUID, since: datetime, db: AsyncSession) -> bool:
    result = await db.execute(
        select(Message.id)
        .where(
            Message.customer_id == customer_id,
            Message.direction == Direction.inbound,
            Message.occurred_at > since,
        )
        .limit(1)
    )
    return result.scalars().first() is not None


async def _daily_limit(business_id: uuid.UUID, connection: ChannelConnection, db: AsyncSession) -> tuple[int, int]:
    """(already sent today, this connection's tier ceiling) - nested-shape
    read, same fix as the messaging-tier bug elsewhere in this codebase."""
    used = await audience_module.sent_today(business_id, db)
    health = (connection.extra or {}).get("health") or {}
    daily = TIER_DAILY_LIMITS.get(health.get("messaging_limit_tier", ""), 250)
    return used, daily


async def send_due_steps(db: AsyncSession) -> int:
    """Send every campaign follow-up step that has become due. Returns how many messages went out."""
    now = datetime.now(timezone.utc)

    campaign_ids = (
        await db.execute(
            select(Campaign.id)
            .join(CampaignStep, CampaignStep.campaign_id == Campaign.id)
            .where(Campaign.status.in_([CampaignStatus.sent, CampaignStatus.paused]))
            .distinct()
        )
    ).scalars().all()

    total_sent = 0
    for campaign_id in campaign_ids:
        try:
            total_sent += await _process_campaign(campaign_id, now, db)
            await db.flush()
        except Exception:
            logger.exception("campaign step sweep failed for campaign %s", campaign_id)
    return total_sent


async def _process_campaign(campaign_id: uuid.UUID, now: datetime, db: AsyncSession) -> int:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        return 0

    steps = (
        await db.execute(
            select(CampaignStep)
            .where(CampaignStep.campaign_id == campaign_id)
            .order_by(CampaignStep.step_order)
        )
    ).scalars().all()
    if not steps:
        return 0

    connection = await _active_connection(campaign.business_id, db)
    if connection is None:
        return 0  # WhatsApp disconnected since this campaign was created - nothing to send with

    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)

    # Step 0 is the campaign's own first send, already recorded in
    # CampaignRecipient - only people it actually reached are eligible for
    # a follow-up; someone it skipped or failed to reach isn't chased by a
    # later step either.
    step0_rows = (
        await db.execute(
            select(CampaignRecipient.customer_id, CampaignRecipient.sent_at).where(
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status == "sent",
            )
        )
    ).all()
    previous_sent_at: dict[uuid.UUID, datetime] = {r.customer_id: r.sent_at for r in step0_rows if r.sent_at}
    if not previous_sent_at:
        return 0

    stopped: set[uuid.UUID] = set()
    sent_count = 0

    for step in steps:
        if not previous_sent_at:
            break

        done_rows = (
            await db.execute(
                select(CampaignStepRecipient.customer_id, CampaignStepRecipient.status).where(
                    CampaignStepRecipient.step_id == step.id
                )
            )
        ).all()
        done_ids = {r.customer_id for r in done_rows}

        due_ids = [
            cid
            for cid, sent_at in previous_sent_at.items()
            if cid not in stopped and cid not in done_ids and now >= sent_at + timedelta(days=step.delay_days)
        ]

        # This step's own successful sends (this sweep's and any earlier
        # sweep's) become the delay anchor for the step after it - step
        # N+1's delay counts from step N's real send time to that person,
        # not from step 0's.
        sent_rows = (
            await db.execute(
                select(CampaignStepRecipient.customer_id, CampaignStepRecipient.sent_at).where(
                    CampaignStepRecipient.step_id == step.id,
                    CampaignStepRecipient.status == "sent",
                )
            )
        ).all()
        next_sent_at = {r.customer_id: r.sent_at for r in sent_rows if r.sent_at}

        if due_ids:
            template = await _template(campaign.business_id, step.template_name, step.template_language, db)
            if template is None:
                for customer_id in due_ids:
                    db.add(
                        CampaignStepRecipient(
                            step_id=step.id, campaign_id=campaign_id, customer_id=customer_id,
                            status="failed", reason=f"Template '{step.template_name}' no longer exists",
                            created_at=now,
                        )
                    )
            else:
                result = await audience_module.resolve(
                    campaign.business_id, campaign.audience, campaign.audience_params, db,
                    require_marketing_opt_in=(_value(template.category) == "MARKETING"),
                )
                by_customer = {r.customer_id: r for r in result.recipients}
                used, daily = await _daily_limit(campaign.business_id, connection, db)

                for customer_id in due_ids:
                    since = previous_sent_at[customer_id]
                    replied = await _replied_since(customer_id, since, db)

                    if replied and step.stop_on_reply:
                        stopped.add(customer_id)
                        continue
                    if step.condition == "no_reply" and replied:
                        db.add(
                            CampaignStepRecipient(
                                step_id=step.id, campaign_id=campaign_id, customer_id=customer_id,
                                status="skipped", reason="Replied since the previous step",
                                created_at=now,
                            )
                        )
                        continue

                    recipient = by_customer.get(customer_id)
                    if recipient is None:
                        # No longer matches the campaign's own audience
                        # question - paid, opted out, or otherwise moved on.
                        # Same discipline as Audience itself: a live
                        # question, not a frozen list.
                        db.add(
                            CampaignStepRecipient(
                                step_id=step.id, campaign_id=campaign_id, customer_id=customer_id,
                                status="skipped", reason="No longer matches this campaign's audience",
                                created_at=now,
                            )
                        )
                        continue

                    if used >= daily:
                        # No row written on purpose: a (step_id, customer_id)
                        # row is unique, so a placeholder here would block
                        # this person from ever being reconsidered. Leaving
                        # them with no row keeps them "due" - the next
                        # sweep tries again, same as tomorrow's fresh tier
                        # allowance.
                        continue

                    variables = [recipient.values.get(k, "") for k in step.variable_mapping]
                    carousel_cards = [
                        CarouselSendCard(
                            media_id=card["media_id"],
                            body_params=[recipient.values.get(k, "") for k in card.get("variable_mapping", [])],
                        )
                        for card in step.carousel_cards
                    ]

                    # Own token per recipient, same mechanism Campaign.flow_id
                    # itself uses - a step can open a Flow too, not only the
                    # campaign's own first send.
                    flow_token = str(uuid.uuid4()) if step.flow_id else None

                    await asyncio.sleep(SEND_PACE_SECONDS)
                    try:
                        outcome = await client.send_template(
                            recipient.phone, step.template_name, step.template_language,
                            body_params=variables or None, carousel_cards=carousel_cards or None,
                            flow_token=flow_token,
                        )
                    except WhatsAppError as exc:
                        db.add(
                            CampaignStepRecipient(
                                step_id=step.id, campaign_id=campaign_id, customer_id=customer_id,
                                status="failed", reason=str(exc), variables=variables, created_at=now,
                            )
                        )
                        continue

                    sent_at = datetime.now(timezone.utc)
                    stored = await ingest.ingest(
                        business_id=campaign.business_id,
                        channel=Channel.whatsapp,
                        direction=Direction.outbound,
                        identity_kind=IdentityKind.phone,
                        identity_value=recipient.phone,
                        external_id=outcome.external_id,
                        text=_fill(template.body_text or "", recipient.values, step.variable_mapping),
                        occurred_at=sent_at,
                        connection_id=connection.id,
                        raw={"campaign_id": str(campaign_id), "step_id": str(step.id), "template": step.template_name},
                        enqueue_analysis=False,
                        db=db,
                    )
                    if flow_token:
                        db.add(
                            FlowSendLog(
                                business_id=campaign.business_id, flow_id=step.flow_id,
                                customer_id=customer_id, flow_token=flow_token,
                            )
                        )
                    db.add(
                        CampaignStepRecipient(
                            step_id=step.id, campaign_id=campaign_id, customer_id=customer_id,
                            status="sent", variables=variables,
                            message_id=stored.message.id if stored.message else None,
                            sent_at=sent_at, created_at=now,
                        )
                    )
                    next_sent_at[customer_id] = sent_at
                    used += 1
                    sent_count += 1

        previous_sent_at = next_sent_at

    return sent_count
