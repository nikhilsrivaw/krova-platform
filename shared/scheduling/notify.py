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

from datetime import datetime, timezone
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
    Commitment,
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
# Cross-vertical, gated on Business.settings["google_review_url"] being
# set - see shared/scheduling/recall.py's send_review_requests.
REVIEW_TEMPLATE_NAME = "review_request"
# order_sync capability (D2C). cod_confirmation's own template must be
# registered in Meta's WhatsApp Manager with two static Quick Reply
# buttons - "Confirm Order" (payload COD_CONFIRM) and "Cancel Order"
# (payload COD_DECLINE) - see shared/care/cod_confirmation.py's own
# docstring for why those exact payload strings matter downstream.
ONBOARDING_NUDGE_TEMPLATE_NAME = "onboarding_nudge"
EXPANSION_NUDGE_TEMPLATE_NAME = "expansion_nudge"
PAYMENT_FAILED_TEMPLATE_NAME = "payment_failed_reminder"
COD_CONFIRMATION_TEMPLATE_NAME = "cod_confirmation"
ABANDONED_CART_TEMPLATE_NAME = "abandoned_cart_recovery"
REPEAT_PURCHASE_TEMPLATE_NAME = "repeat_purchase_nudge"
NDR_RESCHEDULE_TEMPLATE_NAME = "ndr_reschedule_request"
# product_feedback capability (software-startup vertical). WhatsApp-origin
# only - see send_bug_fixed_notification's own docstring for the
# email-origin branch, which does not go through this template mechanism
# at all.
BUG_FIXED_TEMPLATE_NAME = "bug_fixed_notification"


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

    template = None
    if customer.preferred_language:
        # Prefer the customer's own known language when the business has
        # registered a matching-language template - MessageTemplate has
        # always modeled one row per (business, name, language), this is
        # the first time anything actually picks between them. Falls
        # through to the existing unfiltered lookup when no preference is
        # known or no matching-language template was ever registered, so
        # a business with only one language's templates sees no change.
        template = (
            await db.execute(
                select(MessageTemplate).where(
                    MessageTemplate.business_id == business.id,
                    MessageTemplate.name == template_name,
                    MessageTemplate.status == TemplateStatus.approved,
                    MessageTemplate.language == customer.preferred_language,
                )
            )
        ).scalar_one_or_none()
    if template is None:
        # Unlike the language-filtered query above (safe as one-or-none -
        # (business, name, language) is a real unique constraint), this
        # unfiltered fallback can now genuinely match more than one row -
        # a business with both en and hi variants approved and no
        # customer preference to break the tie. Pick deterministically
        # rather than let a business's own multi-language setup crash a
        # send that used to be guaranteed single-row before this feature
        # existed.
        template = (
            await db.execute(
                select(MessageTemplate).where(
                    MessageTemplate.business_id == business.id,
                    MessageTemplate.name == template_name,
                    MessageTemplate.status == TemplateStatus.approved,
                ).order_by(MessageTemplate.language).limit(1)
            )
        ).scalars().first()
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


async def send_review_request(db: AsyncSession, *, business: Business, customer: Customer, review_url: str) -> bool:
    """
    Send the review_request template after a completed visit - cross-
    vertical, gated by the caller (shared/scheduling/recall.py's
    send_review_requests) on Business.settings["google_review_url"]
    actually being set. Generic wording, same "no vertical-specific
    content in a Meta-approved template" reasoning as send_recall_reminder.
    """
    return await _send(
        db, business=business, customer=customer,
        template_name=REVIEW_TEMPLATE_NAME,
        body_params=[customer.display_name or "there", business.name, review_url],
        plain_text=f"Hi, thanks for choosing {business.name}! If you have a moment, we'd really appreciate a quick review: {review_url}",
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


async def send_cod_confirmation(
    db: AsyncSession, *, business: Business, customer: Customer,
    order_number: str, total_paise: int | None,
) -> bool:
    """
    order_sync capability. The template itself must be registered in
    Meta's WhatsApp Manager with two static Quick Reply buttons -
    "Confirm Order" (payload COD_CONFIRM) and "Cancel Order" (payload
    COD_DECLINE) - registered once at template-approval time, not
    something this call passes per-send. See shared/care/
    cod_confirmation.py for what happens when the customer taps one.
    """
    amount = f"₹{total_paise / 100:,.0f}" if total_paise else "the order amount"
    return await _send(
        db, business=business, customer=customer,
        template_name=COD_CONFIRMATION_TEMPLATE_NAME,
        body_params=[customer.display_name or "there", order_number, amount],
        plain_text=f"Please confirm your Cash on Delivery order #{order_number} ({amount}) with {business.name}.",
    )


async def send_abandoned_cart_recovery(
    db: AsyncSession, *, business: Business, customer: Customer, checkout_url: str,
) -> bool:
    """order_sync capability. checkout_url is Shopify's own resume-checkout
    link, sent verbatim - never reconstructed."""
    return await _send(
        db, business=business, customer=customer,
        template_name=ABANDONED_CART_TEMPLATE_NAME,
        body_params=[customer.display_name or "there", business.name, checkout_url],
        plain_text=f"Hi, you left something in your cart at {business.name} - pick up where you left off: {checkout_url}",
    )


async def send_repeat_purchase_nudge(db: AsyncSession, *, business: Business, customer: Customer) -> bool:
    """order_sync capability - 77% of first-time Indian D2C buyers never
    make a second purchase (see docs/d2c-research.md); this is the cheap
    extension of send_review_request's own infrastructure aimed at that
    gap instead of a review."""
    return await _send(
        db, business=business, customer=customer,
        template_name=REPEAT_PURCHASE_TEMPLATE_NAME,
        body_params=[customer.display_name or "there", business.name],
        plain_text=f"Hi, hope you're loving your order from {business.name}! Come back any time - we'd love to have you again.",
    )


async def send_ndr_reschedule_request(
    db: AsyncSession, *, business: Business, customer: Customer, order_number: str,
) -> bool:
    """
    order_sync capability - fires the moment Shiprocket reports a failed
    delivery attempt (NDR). The actual RTO-prevention moment: a customer
    who reschedules here is a parcel that does not go back to origin.
    """
    return await _send(
        db, business=business, customer=customer,
        template_name=NDR_RESCHEDULE_TEMPLATE_NAME,
        body_params=[customer.display_name or "there", order_number],
        plain_text=f"We tried to deliver your order #{order_number} but missed you - reply to let us know when you'll be available.",
    )


async def send_bug_fixed_notification(
    db: AsyncSession, *, business: Business, customer: Customer, commitment: Commitment,
) -> bool:
    """
    Close the loop on a promised bug fix - product_feedback capability.
    Fired only by services/api/routers/webhooks.py's GitHub receiver, once
    the real issue actually closes - never guessed, never sent because an
    LLM decided the bug was probably fixed.

    Unlike every other function in this module, this one is not
    WhatsApp-only: this vertical's users are more often reachable by
    email or the web widget than WhatsApp, so the send channel is
    whichever channel the original bug report actually came in on - the
    same "reply where they wrote" rule identity resolution already
    applies everywhere else in this codebase - read off the Message
    behind the commitment's own first source_message_id.
    """
    from shared.db.models import Channel, EmailSendConnection, Message

    if commitment.bug_fix_notified_at is not None:
        return False  # already sent - see the webhook receiver's own idempotency note

    origin_channel = None
    if commitment.source_message_ids:
        source = await db.get(Message, commitment.source_message_ids[0])
        if source is not None:
            origin_channel = source.channel

    sent = False
    if origin_channel == Channel.whatsapp:
        sent = await _send(
            db, business=business, customer=customer,
            template_name=BUG_FIXED_TEMPLATE_NAME,
            body_params=[customer.display_name or "there", commitment.description],
            plain_text=f"Good news - the issue you reported (\"{commitment.description}\") has been fixed.",
        )
    else:
        # Email/web-origin (or unknown - email is the safer default for a
        # non-WhatsApp report, since a web-widget session is rarely still
        # open by the time a bug is actually fixed).
        from shared.integrations import postmark

        connection = (
            await db.execute(
                select(EmailSendConnection).where(
                    EmailSendConnection.business_id == business.id,
                    EmailSendConnection.verified.is_(True),
                )
            )
        ).scalar_one_or_none()
        to_email = None
        if connection is not None:
            identity = (
                await db.execute(
                    select(CustomerIdentity.value).where(
                        CustomerIdentity.customer_id == customer.id,
                        CustomerIdentity.kind == IdentityKind.email,
                    ).limit(1)
                )
            ).scalar_one_or_none()
            to_email = identity

        if connection is None or to_email is None:
            logger.info(
                "bug-fixed notification skipped business=%s commitment=%s - no verified email connection or customer email",
                business.id, commitment.id,
            )
        else:
            try:
                await postmark.send_email(
                    from_email=connection.from_email,
                    to=to_email,
                    subject=f"Fixed: {commitment.description}",
                    text_body=(
                        f"Hi {customer.display_name or 'there'},\n\n"
                        f"Good news - the issue you reported (\"{commitment.description}\") has been fixed.\n\n"
                        f"- {business.name}"
                    ),
                )
                sent = True
            except postmark.PostmarkError as exc:
                logger.warning("bug-fixed email failed business=%s commitment=%s: %s", business.id, commitment.id, exc)

    if sent:
        commitment.bug_fix_notified_at = datetime.now(timezone.utc)
    return sent


async def _send_whatsapp_or_email(
    db: AsyncSession, *, business: Business, customer: Customer,
    whatsapp_template: str, whatsapp_params: list[str], whatsapp_plain_text: str,
    email_subject: str, email_body: str, log_label: str,
) -> bool:
    """
    Shared dual-channel dispatch behind send_onboarding_nudge and
    send_expansion_nudge below - both need the identical "try WhatsApp,
    fall back to the verified EmailSendConnection" shape with no origin
    message to key off (a lifecycle event has no channel of its own,
    unlike a bug report - see send_bug_fixed_notification, which keys off
    the origin message instead and is kept separate rather than forced
    through this same helper).
    """
    from shared.db.models import EmailSendConnection

    whatsapp_ok = await _send(
        db, business=business, customer=customer,
        template_name=whatsapp_template, body_params=whatsapp_params, plain_text=whatsapp_plain_text,
    )
    if whatsapp_ok:
        return True

    connection = (
        await db.execute(
            select(EmailSendConnection).where(
                EmailSendConnection.business_id == business.id,
                EmailSendConnection.verified.is_(True),
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        return False

    to_email = (
        await db.execute(
            select(CustomerIdentity.value).where(
                CustomerIdentity.customer_id == customer.id,
                CustomerIdentity.kind == IdentityKind.email,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if to_email is None:
        return False

    from shared.integrations import postmark

    try:
        await postmark.send_email(from_email=connection.from_email, to=to_email, subject=email_subject, text_body=email_body)
        return True
    except postmark.PostmarkError as exc:
        logger.warning("%s email failed business=%s customer=%s: %s", log_label, business.id, customer.id, exc)
        return False


async def send_onboarding_nudge(db: AsyncSession, *, business: Business, customer: Customer) -> bool:
    """
    product_feedback capability. Fired by shared/care/onboarding_dropoff.py
    for a customer whose business reported a trial_started
    CustomerLifecycleEvent with no later activated one, past the 3-day
    window research found most predictive.
    """
    return await _send_whatsapp_or_email(
        db, business=business, customer=customer,
        whatsapp_template=ONBOARDING_NUDGE_TEMPLATE_NAME,
        whatsapp_params=[customer.display_name or "there"],
        whatsapp_plain_text=f"Hi, noticed you haven't finished setting up {business.name} yet - need a hand with anything?",
        email_subject=f"Need a hand getting started with {business.name}?",
        email_body=(
            f"Hi {customer.display_name or 'there'},\n\n"
            f"Noticed you haven't finished setting up {business.name} yet - "
            "just reply if you need a hand with anything.\n\n"
            f"- {business.name}"
        ),
        log_label="onboarding nudge",
    )


async def send_expansion_nudge(db: AsyncSession, *, business: Business, customer: Customer) -> bool:
    """
    product_feedback capability. Fired by shared/care/expansion_signals.py
    for a customer whose business reported a usage-milestone
    CustomerLifecycleEvent (e.g. usage_threshold_reached) - top SaaS
    companies get 50%+ of new ARR from existing-customer expansion, and
    research found most companies leave 40-60% of it on the table for
    lack of a system like this. Krova never invents the milestone itself
    - it only ever fires because the business's own product told it one
    was hit (see CustomerLifecycleEvent's own docstring).
    """
    return await _send_whatsapp_or_email(
        db, business=business, customer=customer,
        whatsapp_template=EXPANSION_NUDGE_TEMPLATE_NAME,
        whatsapp_params=[customer.display_name or "there"],
        whatsapp_plain_text=f"Hi, looks like you're getting real value out of {business.name} - want to talk about upgrading?",
        email_subject=f"You're growing with {business.name} - let's talk upgrade",
        email_body=(
            f"Hi {customer.display_name or 'there'},\n\n"
            f"Looks like you're getting real value out of {business.name} - "
            "happy to talk through upgrading if that's useful.\n\n"
            f"- {business.name}"
        ),
        log_label="expansion nudge",
    )


async def send_payment_failed_reminder(
    db: AsyncSession, *, business: Business, customer: Customer, invoice_url: str | None,
) -> bool:
    """
    product_feedback capability. Fired once by services/api/routers/
    webhooks.py's Stripe receiver, the moment invoice.payment_failed
    arrives - never inferred, never sent speculatively. This only makes
    sure the business and the ledger both know a payment is stuck; Stripe's
    own Smart Retries already handle actually recovering it (see
    Commitment(kind=payment) created alongside this - the ledger entry,
    not this message, is the durable record).
    """
    return await _send(
        db, business=business, customer=customer,
        template_name=PAYMENT_FAILED_TEMPLATE_NAME,
        body_params=[customer.display_name or "there", invoice_url or ""],
        plain_text=(
            f"Hi, your last payment to {business.name} didn't go through - "
            + (f"you can update your payment method here: {invoice_url}" if invoice_url else "please update your payment method to keep your account active.")
        ),
    )
