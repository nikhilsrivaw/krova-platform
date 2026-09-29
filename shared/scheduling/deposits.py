"""
Deposit-on-booking - built for the whole Type 4 shape (salons, repair shops,
studios, table-booking restaurants), not any one of them.

Research (docs/new/market-types-research.md, Type 4) found the single
highest-leverage lever in this type: a deposit locked at booking time cuts
no-shows sharply (Indian salons: 65% cut from a 50% deposit). Every business
in this shape wants a different amount and a different trigger - a salon
might deposit-gate every booking, a restaurant only a large party - so this
module reads one generic, business-configured policy rather than assuming
any of that.

No new payment integration. A deposit is an ordinary they-owe Commitment,
requested through the WhatsApp Payments loop services/api/routers/ledger.py's
request-payment endpoint already sends and services/api/routers/webhooks.py's
payment-status handler already verifies and records - Meta-native, the
business's own Razorpay/PayU account, Krova only ever holds the
payment_configuration_id Meta hands back. That loop is flagged UNVERIFIED
AGAINST A LIVE WABA in shared/channels/whatsapp/client.py's own docstring;
this reuses it as-is rather than building a second, parallel path - confirm
it against a real test send before trusting it with a real deposit.

Config shape, Business.settings["scheduling"]["deposit"]:
    {
      "enabled": true,
      "amount_paise": 20000,       # flat amount - no per-service tiers yet
      "template_name": "...",      # an approved WhatsApp order-details template
      "template_language": "en",
      "window_minutes": 120        # how long a slot is held before release
    }
Missing or "enabled" false/absent: no deposit is ever requested, and
book() behaves exactly as it did before this module existed.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt
from shared.channels.whatsapp.client import WhatsAppClient, WhatsAppError
from shared.db.models import (
    Appointment,
    AppointmentStatus,
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
    IdentityKind,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_WINDOW = timedelta(minutes=120)


class DepositError(Exception):
    """A deposit could not be requested - the booking stays awaiting_deposit
    regardless, so staff can see it and send the request manually from the
    Ledger (the same request-payment button this reuses)."""


def deposit_policy(business: Business) -> dict | None:
    """This business's deposit config, or None if deposits are off. The one
    place every caller reads the setting from, so "off" behaves identically
    everywhere."""
    policy = (business.settings or {}).get("scheduling", {}).get("deposit") or {}
    if not policy.get("enabled") or not policy.get("amount_paise"):
        return None
    return policy


async def request_deposit(
    db: AsyncSession, *, business: Business, appointment: Appointment, customer: Customer
) -> Commitment:
    """
    Create the deposit Commitment for a just-booked appointment and try to
    send the WhatsApp payment request for it.

    Always creates and returns the Commitment, and always leaves the
    appointment in awaiting_deposit with a deposit_expires_at set - a send
    failure (no payment method configured, no phone on file, template
    rejected) is logged and raised as DepositError, but never silently
    drops the requirement or auto-confirms the booking. The caller (book())
    does not need to catch this; a failed send is a real thing staff should
    see and can retry manually.
    """
    policy = deposit_policy(business)
    if policy is None:
        raise DepositError("deposit policy is not enabled for this business")

    commitment = Commitment(
        business_id=business.id,
        customer_id=customer.id,
        appointment_id=appointment.id,
        direction=CommitmentDirection.they_owe,
        kind=CommitmentKind.payment,
        description=f"Booking deposit - {appointment.starts_at.strftime('%d %b, %I:%M %p')}",
        amount_paise=policy["amount_paise"],
        status=CommitmentStatus.open,
        confidence=1.0,
        source_message_ids=appointment.source_message_ids or [],
    )
    db.add(commitment)
    appointment.status = AppointmentStatus.awaiting_deposit
    appointment.deposit_expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=policy.get("window_minutes") or _DEFAULT_WINDOW.total_seconds() / 60
    )
    await db.flush()

    try:
        await _send_request(db, business=business, customer=customer, commitment=commitment, policy=policy)
    except DepositError:
        logger.warning(
            "deposit payment request send failed appointment=%s commitment=%s - "
            "booking held awaiting_deposit, needs a manual send from the Ledger",
            appointment.id, commitment.id,
        )
        raise
    return commitment


async def _send_request(
    db: AsyncSession, *, business: Business, customer: Customer, commitment: Commitment, policy: dict
) -> None:
    template_name = policy.get("template_name")
    if not template_name:
        raise DepositError("no template_name configured in the deposit policy")

    connection = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == business.id,
                ChannelConnection.channel == Channel.whatsapp,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()
    if connection is None or not connection.access_token:
        raise DepositError("no active WhatsApp connection")

    payment_configuration_id = (connection.extra or {}).get("payment_configuration_id")
    if not payment_configuration_id:
        raise DepositError("WhatsApp Payments is not set up for this business")

    phone = (
        await db.execute(
            select(CustomerIdentity.value).where(
                CustomerIdentity.customer_id == customer.id,
                CustomerIdentity.kind == IdentityKind.phone,
            )
        )
    ).scalars().first()
    if not phone:
        raise DepositError("this customer has no phone number on file")

    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)
    try:
        result = await client.send_order_details(
            phone, template_name,
            language=policy.get("template_language", "en"),
            reference_id=str(commitment.id),
            payment_configuration=payment_configuration_id,
            amount_paise=commitment.amount_paise,
            description=commitment.description,
        )
    except WhatsAppError as exc:
        raise DepositError(str(exc)) from exc

    logger.info(
        "deposit payment request sent business=%s commitment=%s message=%s",
        business.id, commitment.id, result.external_id,
    )


async def resolve_deposit_paid(db: AsyncSession, *, commitment: Commitment) -> Appointment | None:
    """
    Called after a deposit Commitment is marked met by a Meta-verified
    payment (services/api/routers/webhooks.py's payment-status handler).
    Confirms the booking the deposit secured - the calendar sync, the
    appointment_booked webhook and automation rules that a normal booking
    fires from book() were deliberately skipped while the slot only sat
    awaiting_deposit, so they run here instead, once, now that it's real.

    Returns None (not an error) if this commitment was never a deposit, or
    its appointment is no longer waiting - the common case is every other
    commitment on the Ledger, and calling this unconditionally on every
    paid commitment must stay a cheap no-op for all of them.
    """
    if commitment.appointment_id is None:
        return None
    appointment = await db.get(Appointment, commitment.appointment_id)
    if appointment is None or appointment.status != AppointmentStatus.awaiting_deposit:
        return None

    appointment.status = AppointmentStatus.confirmed
    appointment.deposit_expires_at = None
    await db.flush()
    logger.info("deposit paid, appointment confirmed id=%s commitment=%s", appointment.id, commitment.id)

    business = await db.get(Business, appointment.business_id)
    if business is not None:
        from shared.integrations import google_calendar, webhooks
        from shared.db.models import WebhookEventType

        try:
            await google_calendar.sync_appointment(db, business=business, appointment=appointment, action="upsert")
        except Exception:
            logger.exception("calendar sync failed for deposit-confirmed appointment=%s", appointment.id)
        try:
            await webhooks.dispatch_event(
                db, business_id=business.id, event_type=WebhookEventType.appointment_booked.value,
                payload={
                    "appointment_id": str(appointment.id),
                    "customer_id": str(appointment.customer_id),
                    "doctor_id": str(appointment.doctor_id),
                    "starts_at": appointment.starts_at.isoformat(),
                    "ends_at": appointment.ends_at.isoformat(),
                    "intake_channel": appointment.intake_channel.value,
                },
            )
        except Exception:
            logger.exception("webhook dispatch failed for deposit-confirmed appointment=%s", appointment.id)
        try:
            from shared.care import post_call_actions

            await post_call_actions.apply_rules(
                db, business_id=business.id, trigger_type=WebhookEventType.appointment_booked.value,
                customer_id=appointment.customer_id, channel=appointment.intake_channel.value,
                context={
                    "starts_at": appointment.starts_at.isoformat(),
                    "intake_channel": appointment.intake_channel.value,
                    "deposit_paid": True,
                },
            )
        except Exception:
            logger.exception("automation-rule dispatch failed for deposit-confirmed appointment=%s", appointment.id)

    return appointment


async def release_unpaid_deposits(db: AsyncSession) -> int:
    """
    Sweep: an appointment still awaiting_deposit past its deadline never
    got paid - release the slot rather than hold it forever. Cancels
    through booking.cancel() so calendar/webhook/automation side effects
    and the audit trail (the row stays, status says why) match any other
    cancellation.
    """
    from shared.scheduling import booking

    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(Appointment).where(
                Appointment.status == AppointmentStatus.awaiting_deposit,
                Appointment.deposit_expires_at.is_not(None),
                Appointment.deposit_expires_at < now,
            )
        )
    ).scalars().all()

    released = 0
    for appointment in rows:
        await booking.cancel(db, appointment=appointment, reason="deposit not paid in time")
        released += 1
    if released:
        logger.info("released %s slot(s) held on an unpaid deposit", released)
    return released
