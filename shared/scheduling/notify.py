"""
Reaching the customer about a booking on WhatsApp - confirming it, and
reminding them before it happens.

The one channel that reaches someone whether they booked by voice, by
WhatsApp, or had a staff member enter it manually - and the only way to
reach someone proactively outside the 24-hour service window, which is why
every send here goes through an approved template
(WhatsAppClient.send_template) rather than a free-form message.

Degrades honestly throughout: a business that has not had a template
approved by Meta yet gets a skipped send and a log line, never a crash and
never a fabricated message the business didn't actually send.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt
from shared.channels import ingest
from shared.channels.whatsapp.client import WhatsAppClient, WhatsAppError
from shared.db.models import (
    Business,
    Channel,
    ChannelConnection,
    ConnectionStatus,
    Customer,
    CustomerIdentity,
    Direction,
    Doctor,
    IdentityKind,
    MessageTemplate,
    TemplateStatus,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Names a business must register and get approved with Meta for these to
# work. Documented for the owner during clinic-vertical onboarding, not
# created here - Krova cannot approve a template on a business's behalf.
CONFIRMATION_TEMPLATE_NAME = "appointment_confirmed"
REMINDER_TEMPLATE_NAME = "appointment_reminder"
# Chronic-care recall (care_recall capability) and OPD queue check-in
# (opd_queue capability) - both proactive, both outside the 24h window,
# both go through the same approved-template mechanics as the two above.
RECALL_TEMPLATE_NAME = "recall_reminder"
QUEUE_CHECKIN_TEMPLATE_NAME = "queue_checkin_confirmation"


async def _send(
    db: AsyncSession,
    *,
    business: Business,
    customer: Customer,
    template_name: str,
    body_params: list[str],
    plain_text: str,
) -> bool:
    """
    Shared mechanics behind every appointment-related send: find the active
    WhatsApp connection and the approved template, send it, record it.
    Returns whether it actually sent.
    """
    connection = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == business.id,
                ChannelConnection.channel == Channel.whatsapp,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalar_one_or_none()
    if connection is None or not connection.access_token:
        return False

    template = (
        await db.execute(
            select(MessageTemplate).where(
                MessageTemplate.business_id == business.id,
                MessageTemplate.name == template_name,
                MessageTemplate.status == TemplateStatus.approved,
            )
        )
    ).scalar_one_or_none()
    if template is None:
        logger.info(
            "no approved %s template for business=%s, skipping send",
            template_name, business.id,
        )
        return False

    phone = (
        await db.execute(
            select(CustomerIdentity.value)
            .where(
                CustomerIdentity.customer_id == customer.id,
                CustomerIdentity.kind == IdentityKind.phone,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if phone is None:
        return False

    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)
    try:
        outcome = await client.send_template(
            phone, template_name, template.language, body_params=body_params
        )
    except WhatsAppError as exc:
        logger.warning("%s send failed for business=%s: %s", template_name, business.id, exc)
        return False

    await ingest.ingest(
        business_id=business.id,
        channel=Channel.whatsapp,
        direction=Direction.outbound,
        identity_kind=IdentityKind.phone,
        identity_value=phone,
        external_id=outcome.external_id,
        text=plain_text,
        occurred_at=datetime.now(ZoneInfo("UTC")),
        connection_id=connection.id,
        enqueue_analysis=False,
        db=db,
    )
    return True


def _when(business: Business, starts_at: datetime) -> str:
    return starts_at.astimezone(ZoneInfo(business.timezone)).strftime("%A, %d %B at %I:%M %p")


async def send_confirmation(
    db: AsyncSession, *, business: Business, customer: Customer, doctor: Doctor, starts_at: datetime,
) -> bool:
    """Send the appointment_confirmed template, if this business has one approved and connected."""
    when = _when(business, starts_at)
    return await _send(
        db, business=business, customer=customer,
        template_name=CONFIRMATION_TEMPLATE_NAME,
        body_params=[doctor.name, when],
        plain_text=f"Your appointment with {doctor.name} is confirmed for {when}.",
    )


async def send_reminder(
    db: AsyncSession, *, business: Business, customer: Customer, doctor: Doctor, starts_at: datetime,
) -> bool:
    """Send the appointment_reminder template, if this business has one approved and connected."""
    when = _when(business, starts_at)
    return await _send(
        db, business=business, customer=customer,
        template_name=REMINDER_TEMPLATE_NAME,
        body_params=[doctor.name, when],
        plain_text=f"Reminder: your appointment with {doctor.name} is {when}.",
    )


async def send_recall_reminder(
    db: AsyncSession, *, business: Business, customer: Customer,
) -> bool:
    """
    Send the recall_reminder template for a promised follow-up whose due
    date has arrived - care_recall capability.

    Deliberately generic: no condition, diagnosis, or drug name, per
    clinic.json's policy - the template text itself must stay this way too,
    since Meta reviews and locks the approved template's wording.
    """
    return await _send(
        db, business=business, customer=customer,
        template_name=RECALL_TEMPLATE_NAME,
        body_params=[business.name],
        plain_text=f"Reminder from {business.name}: it's time for your scheduled follow-up. Reply or call us to book a convenient time.",
    )


async def send_queue_checkin(
    db: AsyncSession, *, business: Business, customer: Customer, queue_number: int,
) -> bool:
    """Send the queue_checkin_confirmation template on OPD check-in - opd_queue capability."""
    return await _send(
        db, business=business, customer=customer,
        template_name=QUEUE_CHECKIN_TEMPLATE_NAME,
        body_params=[str(queue_number), business.name],
        plain_text=f"You're #{queue_number} in line at {business.name}. We'll notify you as your turn nears.",
    )
