"""
Which business a phone number belongs to.

The same lookup WhatsApp does on phone_number_id, done on a Plivo number
instead. One number, one business - a call arriving on a number nobody has
connected has nowhere to go, and must be turned away with a clean hangup
rather than routed to whichever business happens to be first in the table.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Business, BusinessDNA, Channel, ChannelConnection, ConnectionStatus


@dataclass(slots=True)
class VoiceRoute:
    business_id: uuid.UUID
    business_name: str
    connection_id: uuid.UUID
    greeting: str
    language: str  # Sarvam language_code, e.g. "hi-IN", "en-IN"
    # "adaptive" (default) replies in whatever the caller spoke, per-turn -
    # see relay.py's own comment on why that was the original design. "fixed"
    # overrides that and always replies in `language`, for a business that
    # wants e.g. a Hindi-only agent regardless of what a caller mixes in.
    language_mode: str
    # One of Sarvam bulbul:v3's real speaker names (e.g. "shubh", "priya") -
    # never invented, always checked against Sarvam's own published list.
    speaker: str
    # E.164 without the leading "+", matching CustomerIdentity's own phone
    # storage convention - a real person's number to bring onto a call,
    # either for a warm transfer on escalate or (with copilot_mode) as who
    # answers every call directly. None means neither feature is in use for
    # this business, which is the default and preserves today's behaviour
    # exactly: escalate stays an apology-and-hangup, every call stays
    # AI-answered.
    staff_phone_number: str | None
    # True only when a business has explicitly opted in - never a default.
    # Meaningless without staff_phone_number also being set; the caller
    # (answer.py) checks both together.
    copilot_mode: bool


async def resolve(to_number: str, db: AsyncSession) -> VoiceRoute | None:
    """
    Find the business that owns the number a caller dialled.

    Returns None for an unrecognised number - happens if a number is
    disconnected while a call is already ringing, or if something outside
    Krova is pointed at this webhook by mistake.
    """
    normalised = "".join(ch for ch in to_number if ch.isdigit())

    result = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.channel == Channel.voice,
            ChannelConnection.status == ConnectionStatus.active,
            ChannelConnection.external_account_id.in_(
                {to_number, normalised, f"+{normalised}"}
            ),
        )
    )
    connection = result.scalars().first()
    if connection is None:
        return None

    business = await db.get(Business, connection.business_id)
    if business is None or not business.is_active:
        return None

    dna = await db.get(BusinessDNA, business.id)
    extra = connection.extra or {}

    return VoiceRoute(
        business_id=business.id,
        business_name=business.name,
        connection_id=connection.id,
        greeting=extra.get("greeting")
        or f"Hello, thank you for calling {business.name}. How can I help you?",
        language=extra.get("language", "en-IN"),
        language_mode=extra.get("language_mode", "adaptive"),
        speaker=extra.get("speaker", "shubh"),
        staff_phone_number=extra.get("staff_phone_number") or None,
        copilot_mode=bool(extra.get("copilot_mode", False)),
    )
