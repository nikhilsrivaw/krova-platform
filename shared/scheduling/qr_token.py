"""
A token from the reception QR code, without the AI.

The clinic prints a QR code that opens WhatsApp to its own number with a message already typed ("Mujhe aaj ka token
chahiye"). The patient only taps Send. When exactly that message arrives and a shift is open, the token is issued here,
straight away: no AI call (fast, sure and free), the phone number is the patient's own WhatsApp, and because the
patient has just written, the reply, the payment link and the "your turn is near" alert all go as plain messages - no
template needed.

The same patient scanning twice keeps the token they have. With two shifts open at once (say morning running late), the
token goes to the shift that is running now by the clock; the emergency track is never given out this way.
"""

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared import verticals
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
    IdentityKind,
    IntakeChannel,
    Message,
    QueueEntry,
    QueueStatus,
    Shift,
)
from shared.scheduling import queue_booking
from shared.verticals import labels as vertical_labels
from shared.utils.logging import get_logger

logger = get_logger(__name__)

DEFAULT_PHRASE = "Mujhe aaj ka token chahiye"
# Before this hour (business's own clock) a morning shift is the one running; after it, an evening shift.
EVENING_FROM_HOUR = 14


def phrase_for(business: Business) -> str:
    raw = ((business.settings or {}).get("queue") or {}).get("qr_phrase")
    return raw.strip() if isinstance(raw, str) and raw.strip() else DEFAULT_PHRASE


def _normal(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


def matches(text: str, phrase: str) -> bool:
    """The QR's own message, give or take case, spaces and punctuation - not any message that mentions a token."""
    return bool(_normal(phrase)) and _normal(text) == _normal(phrase)


def pick_shift(open_shifts: list[Shift], local_hour: int) -> Shift | None:
    """The shift to give a QR token in, or None. Never the emergency track."""
    regular = [s for s in open_shifts if s != Shift.emergency]
    if not regular:
        return None
    if len(regular) == 1:
        return regular[0]
    preferred = Shift.morning if local_hour < EVENING_FROM_HOUR else Shift.evening
    return preferred if preferred in regular else regular[0]


def token_text(*, business_name: str, ticket: str, shift_label: str, number: int, ahead: int) -> str:
    people = "aapse pehle koi nahi" if ahead == 0 else f"aapse pehle {ahead} log hain"
    ahead_en = "you're next" if ahead == 0 else f"{ahead} ahead of you"
    return (
        f"{business_name}: aapka {ticket} #{number} hai ({shift_label}), {people}. "
        f"Baari paas aane par hum yahin WhatsApp karenge.\n"
        f"Your {ticket} is #{number} ({shift_label}), {ahead_en}. We'll message you here as your turn nears."
    )


CLOSED_TEXT = (
    "Abhi koi shift khuli nahi hai, isliye token abhi nahi mil sakta. Clinic khulne par dobara bhejiye, "
    "ya reception par puchhiye.\nNo shift is open right now - please send this again when the clinic opens, "
    "or ask at reception."
)


async def _reply(db: AsyncSession, business: Business, customer: Customer, text: str) -> bool:
    connection = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == business.id,
                ChannelConnection.channel == Channel.whatsapp,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()
    phone = (
        await db.execute(
            select(CustomerIdentity.value).where(
                CustomerIdentity.customer_id == customer.id, CustomerIdentity.kind == IdentityKind.phone,
            )
        )
    ).scalars().first()
    if connection is None or not connection.access_token or not phone:
        return False
    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)
    try:
        result = await client.send_text(phone, text)
    except WhatsAppError as exc:
        logger.warning("qr token reply not sent business=%s: %s", business.id, exc)
        return False
    await ingest.ingest(
        business_id=business.id, channel=Channel.whatsapp, direction=Direction.outbound,
        identity_kind=IdentityKind.phone, identity_value=phone, external_id=result.external_id,
        text=text, occurred_at=datetime.now(timezone.utc), connection_id=connection.id, enqueue_analysis=False,
        db=db,
    )
    return True


async def _waiting_token_today(db: AsyncSession, business_id, customer_id, shift: Shift) -> QueueEntry | None:
    today = datetime.now(timezone.utc).date()
    return (
        await db.execute(
            select(QueueEntry).where(
                QueueEntry.business_id == business_id, QueueEntry.customer_id == customer_id,
                QueueEntry.queue_date == today, QueueEntry.shift == shift,
                QueueEntry.status.in_([QueueStatus.waiting, QueueStatus.in_consultation]),
            )
        )
    ).scalars().first()


async def _ahead(db: AsyncSession, entry: QueueEntry) -> int:
    rows = (
        await db.execute(
            select(QueueEntry.id).where(
                QueueEntry.business_id == entry.business_id, QueueEntry.queue_date == entry.queue_date,
                QueueEntry.shift == entry.shift, QueueEntry.status == QueueStatus.waiting,
                QueueEntry.queue_number < entry.queue_number,
            )
        )
    ).scalars().all()
    return len(rows)


async def handle(message: Message, business: Business, customer: Customer, db: AsyncSession) -> bool:
    """
    True when this message was the reception QR's and has been answered here (the AI is then skipped).
    False for every other message, which goes to the AI as usual.
    """
    channel = message.channel.value if hasattr(message.channel, "value") else message.channel
    if channel != "whatsapp" or not verticals.has_capability(business, "opd_queue"):
        return False
    if not matches(message.content or "", phrase_for(business)):
        return False

    sessions = await queue_booking.open_shifts_today(db, business_id=business.id)
    local_hour = datetime.now(ZoneInfo(business.timezone or "Asia/Kolkata")).hour
    shift = pick_shift([s.shift for s in sessions], local_hour)
    if shift is None:
        await _reply(db, business, customer, CLOSED_TEXT)
        return True

    entry = await _waiting_token_today(db, business.id, customer.id, shift)
    if entry is None:
        try:
            entry = await queue_booking.issue_token(
                db, business_id=business.id, shift=shift, customer_id=customer.id, doctor_id=None,
                intake_channel=IntakeChannel.whatsapp, source_message_ids=[message.id],
                notify_customer=False,  # the reply below says it, once
            )
        except (queue_booking.ShiftNotOpen, queue_booking.OpdQueueNotEnabled):
            await _reply(db, business, customer, CLOSED_TEXT)
            return True

    from shared.payments import links as payment_links

    words = vertical_labels.queue_labels(business)
    text = token_text(
        business_name=business.name, ticket=words.get("ticket", "token"),
        shift_label=(words.get("shifts") or {}).get(shift.value, shift.value),
        number=entry.queue_number, ahead=await _ahead(db, entry),
    )
    fee = payment_links.auto_settings(business)["default_fee_paise"]
    if payment_links.payment_mode(business) == "counter" and fee:
        # the business takes the fee at the counter: say how much, so nobody is surprised there
        text += f"\nFees {payment_links.rupees(fee)} counter par dein / Please pay {payment_links.rupees(fee)} at the counter."
    await _reply(db, business, customer, text)

    await payment_links.auto_link_after_booking(db, business=business, kind="tokens", ref_id=entry.id)
    logger.info("qr token business=%s customer=%s number=%s", business.id, customer.id, entry.queue_number)
    return True
