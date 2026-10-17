"""
Sending a payment link on WhatsApp, and noticing when it is paid.

A link always collects one Commitment the customer owes (a fee, an advance, a balance). Sending: the link is created in
the business's own gateway, then sent as a plain WhatsApp message when the customer wrote in the last 24 hours, or with
an approved template (two variables: the amount, the link) when they did not - WhatsApp only lets a business open a
conversation with a template. The message is written to the conversation like any message staff send.

Noticing payment: sync_open_links asks the gateway about open links every few minutes, so the business never has to
set up a webhook. Money the gateway reports is written to the Commitment with record_payment; recorded_paise makes a
repeated check harmless. KROVA never marks anything paid on a customer's word or screenshot - only on the gateway's.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt, encrypt
from shared.care.commitment_payments import PaymentError, record_payment
from shared.channels import ingest
from shared.channels.whatsapp.client import WhatsAppClient, WhatsAppError, within_service_window
from shared.db.models import (
    Business,
    Channel,
    ChannelConnection,
    Commitment,
    CommitmentDirection,
    CommitmentKind,
    CommitmentStatus,
    ConnectionStatus,
    Customer,
    CustomerIdentity,
    Direction,
    IdentityKind,
    Message,
    MessageTemplate,
    PaymentGatewayConnection,
    PaymentLink,
    TemplateStatus,
)
from shared.payments.gateways import GatewayError, gateway_for
from shared.utils.logging import get_logger

logger = get_logger(__name__)

OPEN_STATUSES = ("created", "partially_paid")
# Links are created to last 7 days; checking a little past that catches a payment made at the last minute.
CHECK_WINDOW = timedelta(days=8)
SYNC_BATCH = 200


class LinkError(Exception):
    """A link could not be made or sent; the message is safe to show the owner."""


def rupees(paise: int) -> str:
    value = paise / 100
    return f"Rs {value:,.0f}" if value == int(value) else f"Rs {value:,.2f}"


# ── the gateway key ──────────────────────────────────────────────────────────

async def connection_for(db: AsyncSession, business_id: uuid.UUID) -> PaymentGatewayConnection | None:
    return (
        await db.execute(select(PaymentGatewayConnection).where(PaymentGatewayConnection.business_id == business_id))
    ).scalar_one_or_none()


async def connect(db: AsyncSession, business_id: uuid.UUID, *, gateway: str, key_id: str, key_secret: str) -> PaymentGatewayConnection:
    """Check the key against the gateway first; only a key that works is saved."""
    key_id, key_secret = key_id.strip(), key_secret.strip()
    if not key_id or not key_secret:
        raise LinkError("Enter both the Key ID and the Key Secret.")
    try:
        await gateway_for(gateway, key_id, key_secret).check()
    except GatewayError as exc:
        raise LinkError(str(exc)) from exc
    row = await connection_for(db, business_id)
    now = datetime.now(timezone.utc)
    if row is None:
        row = PaymentGatewayConnection(business_id=business_id, gateway=gateway, key_id=key_id, key_secret=encrypt(key_secret))
        db.add(row)
    else:
        row.gateway, row.key_id, row.key_secret = gateway, key_id, encrypt(key_secret)
    row.status, row.connected_at = ConnectionStatus.active, now
    await db.flush()
    return row


def _client(row: PaymentGatewayConnection):
    return gateway_for(row.gateway, row.key_id, decrypt(row.key_secret))


# ── a fee as something the customer owes ─────────────────────────────────────

def fee_commitment(
    *, business_id: uuid.UUID, customer_id: uuid.UUID, amount_paise: int, description: str,
    appointment_id: uuid.UUID | None = None,
) -> Commitment:
    """A fee written by staff (a token's or an appointment's), as an ordinary open they-owe payment."""
    return Commitment(
        business_id=business_id,
        customer_id=customer_id,
        appointment_id=appointment_id,
        direction=CommitmentDirection.they_owe,
        kind=CommitmentKind.payment,
        description=description[:300],
        amount_paise=amount_paise,
        status=CommitmentStatus.open,
        confidence=1.0,
        source_message_ids=[],
        due_at=datetime.now(timezone.utc),
    )


# ── sending ──────────────────────────────────────────────────────────────────

async def _whatsapp(db: AsyncSession, business_id: uuid.UUID) -> ChannelConnection:
    row = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == business_id,
                ChannelConnection.channel == Channel.whatsapp,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()
    if row is None or not row.access_token:
        raise LinkError("Connect WhatsApp first.")
    return row


async def _phone(db: AsyncSession, customer_id: uuid.UUID) -> str | None:
    return (
        await db.execute(
            select(CustomerIdentity.value).where(
                CustomerIdentity.customer_id == customer_id, CustomerIdentity.kind == IdentityKind.phone,
            )
        )
    ).scalars().first()


async def _last_inbound(db: AsyncSession, customer_id: uuid.UUID) -> datetime | None:
    return (
        await db.execute(
            select(Message.occurred_at)
            .where(Message.customer_id == customer_id, Message.direction == Direction.inbound)
            .order_by(Message.occurred_at.desc())
            .limit(1)
        )
    ).scalars().first()


def message_text(description: str, amount_paise: int, url: str) -> str:
    return f"{description}: {rupees(amount_paise)}\nPay here / yahan pay karein: {url}"


async def send_link(
    db: AsyncSession, *, business: Business, commitment: Commitment, user_id: uuid.UUID | None,
    template_name: str | None = None, template_language: str = "en",
) -> tuple[PaymentLink, bool, str | None]:
    """
    Create a link for what is still owed and send it. Returns (link, sent, reason_not_sent): when WhatsApp refuses,
    the link still exists, so staff can copy it and share it another way.
    """
    if commitment.business_id != business.id:
        raise LinkError("Not found.")
    if commitment.direction != CommitmentDirection.they_owe or commitment.status != CommitmentStatus.open:
        raise LinkError("A payment link is only for something the customer still owes.")
    amount = commitment.outstanding_paise
    if not amount:
        raise LinkError("There is no amount left to collect on this.")
    gateway_row = await connection_for(db, business.id)
    if gateway_row is None or gateway_row.status != ConnectionStatus.active:
        raise LinkError("Connect your payment gateway in Settings first.")

    customer = await db.get(Customer, commitment.customer_id)
    if customer is None:
        raise LinkError("Customer not found.")
    phone = await _phone(db, customer.id)
    wa = await _whatsapp(db, business.id)
    in_window = within_service_window(await _last_inbound(db, customer.id))
    template = None
    if not in_window:
        if not template_name:
            raise LinkError(
                "This customer has not written in the last 24 hours, so WhatsApp needs an approved template. "
                "Pick one with two variables: the amount and the link."
            )
        template = (
            await db.execute(
                select(MessageTemplate).where(
                    MessageTemplate.business_id == business.id,
                    MessageTemplate.name == template_name, MessageTemplate.language == template_language,
                )
            )
        ).scalars().first()
        if template is None or template.status != TemplateStatus.approved:
            raise LinkError(f"The template '{template_name}' is not approved yet.")

    try:
        info = await _client(gateway_row).create_link(
            amount_paise=amount, description=commitment.description, customer_name=customer.display_name,
            customer_phone=phone,
        )
    except GatewayError as exc:
        raise LinkError(str(exc)) from exc
    link = PaymentLink(
        business_id=business.id, commitment_id=commitment.id, gateway=gateway_row.gateway, external_id=info.external_id,
        short_url=info.short_url, amount_paise=amount, status=info.status, expires_at=info.expires_at,
    )
    db.add(link)
    await db.flush()

    if not phone:
        return link, False, "This customer has no phone number on file."
    if getattr(customer, "is_private", False):
        return link, False, "This is a private conversation."
    client = WhatsAppClient(decrypt(wa.access_token), wa.external_account_id)
    try:
        if template is not None:
            params = [rupees(amount), info.short_url]
            result = await client.send_template(phone, template.name, template.language, body_params=params)
            text = template.body_text or template.name
            for i, value in enumerate(params, start=1):
                text = text.replace(f"{{{{{i}}}}}", value)
        else:
            text = message_text(commitment.description, amount, info.short_url)
            result = await client.send_text(phone, text)
    except WhatsAppError as exc:
        logger.warning("payment link %s made but not sent: %s", link.id, exc)
        return link, False, str(exc)[:200]

    await ingest.ingest(
        business_id=business.id, channel=Channel.whatsapp, direction=Direction.outbound,
        identity_kind=IdentityKind.phone, identity_value=phone, external_id=result.external_id,
        text=text, occurred_at=datetime.now(timezone.utc), connection_id=wa.id, enqueue_analysis=False,
        sent_by_user_id=user_id, db=db,
    )
    logger.info("payment link sent business=%s commitment=%s link=%s", business.id, commitment.id, link.external_id)
    return link, True, None


# ── sent by itself after the AI books ────────────────────────────────────────

def auto_settings(business: Business) -> dict:
    """
    Business.settings["payments"]["auto_link"]: {"tokens": bool, "appointments": bool, "default_fee_paise": int|None}.
    Off unless the business switches it on.
    """
    raw = ((business.settings or {}).get("payments") or {}).get("auto_link") or {}
    fee = raw.get("default_fee_paise")
    return {
        "tokens": raw.get("tokens") is True,
        "appointments": raw.get("appointments") is True,
        "default_fee_paise": fee if isinstance(fee, int) and fee >= 100 else None,
    }


async def auto_link_after_booking(
    db: AsyncSession, *, business: Business, customer_id: uuid.UUID, kind: str, description: str,
    doctor_fee_paise: int | None = None, appointment_id: uuid.UUID | None = None,
) -> PaymentLink | None:
    """
    When the AI has just booked a token or an appointment on WhatsApp, send the fee link straight after - if the
    business switched it on and its gateway is connected. The fee is the doctor's consultation fee, else the business's
    default fee. Never raises: a booking that worked must not fail because a link could not be sent.
    """
    try:
        settings_ = auto_settings(business)
        if not settings_.get(kind):
            return None
        amount = doctor_fee_paise or settings_["default_fee_paise"]
        gateway_row = await connection_for(db, business.id)
        if not amount or gateway_row is None or gateway_row.status != ConnectionStatus.active:
            return None
        commitment = fee_commitment(
            business_id=business.id, customer_id=customer_id, amount_paise=amount, description=description,
            appointment_id=appointment_id,
        )
        db.add(commitment)
        await db.flush()
        link, sent, reason = await send_link(db, business=business, commitment=commitment, user_id=None)
        if not sent:
            logger.info("auto payment link made but not sent business=%s: %s", business.id, reason)
        return link
    except Exception:  # noqa: BLE001 - the booking stands whatever happens here
        logger.exception("auto payment link failed business=%s kind=%s", business.id, kind)
        return None


# ── noticing payment ─────────────────────────────────────────────────────────

def apply_status(link: PaymentLink, commitment: Commitment | None, *, status: str, amount_paid_paise: int,
                 now: datetime) -> int:
    """
    Bring a link up to what the gateway says, and write any new money to its Commitment.
    Returns the paise newly recorded. Pure: no database, no network.
    """
    link.status, link.amount_paid_paise, link.last_checked_at = status, amount_paid_paise, now
    if status == "paid" and link.paid_at is None:
        link.paid_at = now
    new = amount_paid_paise - (link.recorded_paise or 0)
    if new <= 0:
        return 0
    link.recorded_paise = amount_paid_paise
    if commitment is None or commitment.status != CommitmentStatus.open:
        # Already closed by hand (or removed): the money is the business's either way; nothing to add.
        return 0
    try:
        record_payment(commitment, amount_paise=new, source=f"{link.gateway}_link", now=now)
    except PaymentError:
        logger.warning("could not record link payment link=%s commitment=%s", link.id, commitment.id)
        return 0
    return new


async def refresh_link(db: AsyncSession, link: PaymentLink, gateway_row: PaymentGatewayConnection, now: datetime) -> int:
    try:
        info = await _client(gateway_row).fetch_link(link.external_id)
    except GatewayError as exc:
        logger.info("payment link check failed link=%s: %s", link.id, exc)
        link.last_checked_at = now
        return 0
    commitment = await db.get(Commitment, link.commitment_id)
    return apply_status(link, commitment, status=info.status, amount_paid_paise=info.amount_paid_paise, now=now)


async def sync_open_links(db: AsyncSession, now: datetime | None = None) -> int:
    """Ask the gateway about every open link from the last few days. Returns how many links took new money."""
    now = now or datetime.now(timezone.utc)
    links = (
        await db.execute(
            select(PaymentLink)
            .where(PaymentLink.status.in_(OPEN_STATUSES), PaymentLink.created_at >= now - CHECK_WINDOW)
            .order_by(PaymentLink.last_checked_at.asc().nullsfirst())
            .limit(SYNC_BATCH)
        )
    ).scalars().all()
    gateways: dict[uuid.UUID, PaymentGatewayConnection | None] = {}
    paid = 0
    for link in links:
        if link.business_id not in gateways:
            gateways[link.business_id] = await connection_for(db, link.business_id)
        row = gateways[link.business_id]
        if row is None or row.status != ConnectionStatus.active:
            continue
        if await refresh_link(db, link, row, now):
            paid += 1
    await db.flush()
    return paid
