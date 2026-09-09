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

from shared.db.models import Business, Channel, ChannelConnection, ConnectionStatus

# Speaker gender, Sarvam bulbul:v3's real published catalogue (never
# invented) - the same lists services/api/routers/voice_provisioning.py
# exposes as male_speakers/female_speakers, kept here too since this is
# where a speaker's gender needs to be known to pick a grammatically
# correct default greeting (see DEFAULT_GREETING_GENDERED below) before a
# business has typed their own.
MALE_SPEAKERS = [
    "shubh", "aditya", "rahul", "rohan", "amit", "dev", "ratan", "varun",
    "manan", "sumit", "kabir", "aayan", "ashutosh", "advait", "anand",
    "tarun", "sunny", "mani", "gokul", "vijay", "mohit", "rehan", "soham",
]
FEMALE_SPEAKERS = [
    "ritu", "priya", "neha", "pooja", "simran", "kavya", "ishita", "shreya",
    "roopa", "tanya", "shruti", "suhani", "kavitha", "rupali",
]

# The un-customised, spoken-live default greeting - was hardcoded English
# regardless of a business's chosen language, so a Hindi/Tamil/etc. agent
# that hadn't typed its own greeting yet opened every real call in English.
# Hindi, Marathi and Punjabi mark the speaker's own gender on "can help" -
# see voice_provisioning.py's _PREVIEW_TEXT_GENDERED for the same split and
# its full reasoning. Reviewed for plausibility, not confirmed by a native
# speaker of each language - same honesty as the preview text.
DEFAULT_GREETING: dict[str, str] = {
    "en-IN": "Hello, thank you for calling {name}. How can I help you?",
    "bn-IN": "নমস্কার, {name}-এ কল করার জন্য ধন্যবাদ। আমি আপনাকে কীভাবে সাহায্য করতে পারি?",
    "gu-IN": "નમસ્તે, {name} પર કૉલ કરવા બદલ આભાર. હું તમારી કેવી રીતે મદદ કરી શકું?",
    "kn-IN": "ನಮಸ್ಕಾರ, {name} ಗೆ ಕರೆ ಮಾಡಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು. ನಾನು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು?",
    "ml-IN": "നമസ്കാരം, {name}-ലേക്ക് വിളിച്ചതിന് നന്ദി. ഞാൻ നിങ്ങളെ എങ്ങനെ സഹായിക്കും?",
    "od-IN": "ନମସ୍କାର, {name} କୁ କଲ୍ କରିଥିବାରୁ ଧନ୍ୟବାଦ। ମୁଁ ଆପଣଙ୍କୁ କିପରି ସାହାଯ୍ୟ କରିପାରିବି?",
    "ta-IN": "வணக்கம், {name}-ஐ அழைத்ததற்கு நன்றி. நான் உங்களுக்கு எப்படி உதவ முடியும்?",
    "te-IN": "నమస్కారం, {name}కి కాల్ చేసినందుకు ధన్యవాదాలు. నేను మీకు ఎలా సహాయం చేయగలను?",
}
DEFAULT_GREETING_GENDERED: dict[str, dict[str, str]] = {
    "hi-IN": {
        "male": "नमस्ते, {name} को कॉल करने के लिए धन्यवाद। मैं आपकी कैसे मदद कर सकता हूं?",
        "female": "नमस्ते, {name} को कॉल करने के लिए धन्यवाद। मैं आपकी कैसे मदद कर सकती हूं?",
    },
    "mr-IN": {
        "male": "नमस्कार, {name} ला कॉल केल्याबद्दल धन्यवाद. मी तुम्हाला कशी मदत करू शकतो?",
        "female": "नमस्कार, {name} ला कॉल केल्याबद्दल धन्यवाद. मी तुम्हाला कशी मदत करू शकते?",
    },
    "pa-IN": {
        "male": "ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ, {name} ਨੂੰ ਕਾਲ ਕਰਨ ਲਈ ਧੰਨਵਾਦ। ਮੈਂ ਤੁਹਾਡੀ ਕਿਵੇਂ ਮਦਦ ਕਰ ਸਕਦਾ ਹਾਂ?",
        "female": "ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ, {name} ਨੂੰ ਕਾਲ ਕਰਨ ਲਈ ਧੰਨਵਾਦ। ਮੈਂ ਤੁਹਾਡੀ ਕਿਵੇਂ ਮਦਦ ਕਰ ਸਕਦੀ ਹਾਂ?",
    },
}


def default_greeting(language: str, speaker: str, business_name: str) -> str:
    """The greeting spoken when a business hasn't typed its own - correct
    for the chosen language and, where the language's grammar requires it,
    the chosen speaker's gender."""
    gendered = DEFAULT_GREETING_GENDERED.get(language)
    if gendered is not None:
        gender = "female" if speaker in FEMALE_SPEAKERS else "male"
        template = gendered[gender]
    else:
        template = DEFAULT_GREETING.get(language, DEFAULT_GREETING["en-IN"])
    return template.format(name=business_name)


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
    # Business.owner_phone, carried onto the route so relay.py can compare
    # it against an inbound caller's number without a second DB fetch -
    # see relay.py's own use of this for the owner voice interface. None
    # (the default) means no owner number is configured, same as today.
    owner_phone: str | None


def _build_route(business: Business, connection: ChannelConnection) -> VoiceRoute:
    """The one place a ChannelConnection's `extra` becomes a VoiceRoute - used by both lookup directions below."""
    extra = connection.extra or {}
    language = extra.get("language", "en-IN")
    speaker = extra.get("speaker", "shubh")
    return VoiceRoute(
        business_id=business.id,
        business_name=business.name,
        connection_id=connection.id,
        greeting=extra.get("greeting")
        or default_greeting(language, speaker, business.name),
        language=language,
        language_mode=extra.get("language_mode", "adaptive"),
        speaker=speaker,
        staff_phone_number=extra.get("staff_phone_number") or None,
        copilot_mode=bool(extra.get("copilot_mode", False)),
        owner_phone=business.owner_phone,
    )


async def resolve(to_number: str, db: AsyncSession) -> VoiceRoute | None:
    """
    Find the business that owns the number a caller dialled - the inbound
    direction, where all we start with is the number Plivo says was
    called.

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

    return _build_route(business, connection)


async def resolve_by_business(business_id: uuid.UUID, db: AsyncSession) -> VoiceRoute | None:
    """
    The outbound direction - KROVA already knows which business is
    calling (from the CallCampaign it placed the call for), so this skips
    the phone-number lookup `resolve()` needs for inbound entirely, going
    straight to that business's active voice connection.
    """
    business = await db.get(Business, business_id)
    if business is None or not business.is_active:
        return None

    result = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == business_id,
            ChannelConnection.channel == Channel.voice,
            ChannelConnection.status == ConnectionStatus.active,
        )
    )
    connection = result.scalars().first()
    if connection is None:
        return None

    return _build_route(business, connection)
