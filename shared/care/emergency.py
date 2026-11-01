"""
An emergency in a message - "papa ko seene mein dard ho raha hai", "सांस नहीं आ रही", "accident ho gaya" - answered at
once with where to get help, on every channel and whatever the autonomy setting, before the AI is asked anything.

Why not leave it to the AI: the healthcare templates told it to "tell them to call emergency services", but an
escalation on a call speaks a fixed "someone will follow up" line, and on WhatsApp in Draft mode nothing is sent at all
until a person replies. An emergency cannot wait for either.

Which businesses: a template opts in with "emergency": "<set>" (shared/verticals/templates/*.json). Only the
healthcare types do today; every other type has none and nothing here runs for it. The business can add its own
emergency desk number (Settings), said after 112.

The phrases are deliberately narrow - "khoon ki jaanch" is a blood test and "emergency shift kab hai" a question, so
bare "khoon", "bleeding" and "emergency" are not on the list. They match as plain substrings, Devanagari included,
because a call's speech-to-text writes Hindi in Devanagari.
"""

import re

from shared.team import language as lang
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Each set in three parts, so the reply can answer in the words they used.
_MEDICAL_ENGLISH = (
        "chest pain", "heart attack", "can't breathe", "cant breathe", "cannot breathe", "not breathing",
        "difficulty breathing", "trouble breathing", "unconscious", "fainted", "collapsed", "severe bleeding",
        "bleeding heavily", "bleeding a lot", "lot of blood", "had an accident", "met with an accident", "stroke",
        "seizure", "having fits", "swallowed poison", "drank poison", "overdose", "suicide", "kill myself",
        "medical emergency", "it's an emergency", "its an emergency", "this is an emergency",
)
_MEDICAL_HINGLISH = (
        "seene mein dard", "seene me dard", "sine me dard", "chhati mein dard", "chhati me dard", "saans nahi aa",
        "sans nahi aa", "saans nahi le", "saans lene mein dikkat", "saans lene me dikkat", "saans phool", "behosh",
        "khoon beh", "bahut khoon", "khoon nahi ruk", "accident ho gaya", "accident hua", "accident ho gya",
        "dil ka daura", "lakwa", "laqwa", "daura pad", "daura padh", "mirgi", "fits aa", "zeher kha", "zahar kha",
        "zehar kha", "khudkushi", "marna chahta", "marna chahti", "emergency hai", "emergency h ",
)
_MEDICAL_HINDI = (
        "सीने में दर्द", "छाती में दर्द", "सांस नहीं", "साँस नहीं", "सांस लेने में", "साँस लेने में", "बेहोश",
        "खून बह", "बहुत खून", "खून नहीं रुक", "एक्सीडेंट", "दुर्घटना", "हार्ट अटैक", "दिल का दौरा", "लकवा",
        "दौरा पड़", "दौरा पड", "मिर्गी", "ज़हर", "जहर खा", "आत्महत्या", "खुदकुशी", "इमरजेंसी है", "मरना चाहत",
)
SETS: dict[str, dict[str, tuple[str, ...]]] = {
    "medical": {"english": _MEDICAL_ENGLISH, "hinglish": _MEDICAL_HINGLISH, "hindi": _MEDICAL_HINDI},
}

_REPLY = {
    "english": "This sounds like an emergency. Please call 112 right now, or go to the nearest emergency room. "
               "I've alerted our team.",
    "hinglish": "Ye emergency lag rahi hai. Abhi 112 par call karein, ya sabse paas ke emergency mein jaayein. "
                "Maine team ko bata diya hai.",
    "hindi": "यह इमरजेंसी लगती है। अभी 112 पर कॉल करें, या सबसे पास की इमरजेंसी में जाएँ। मैंने टीम को बता दिया है।",
}
_OWN_NUMBER = {
    "english": " Our emergency desk: {number}.",
    "hinglish": " Hamara emergency number: {number}.",
    "hindi": " हमारा इमरजेंसी नंबर: {number}।",
}
PHONE_MAX = 20
_PHONE = re.compile(r"^\+?[0-9][0-9 \-]{4,18}[0-9]$")


def _set(business) -> dict[str, tuple[str, ...]]:
    from shared import verticals

    vertical = getattr(business, "vertical", None)
    if not vertical:
        return {}
    try:
        name = verticals.get(vertical).get("emergency")
    except verticals.UnknownVertical:
        return {}
    return SETS.get(name, {}) if isinstance(name, str) else {}


def phrases(business) -> tuple[str, ...]:
    """The emergency phrases this business's type uses - none for most types."""
    return tuple(p for group in _set(business).values() for p in group)


def _said(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").split()) + " "


def detect(business, text: str | None) -> str | None:
    """The phrase that makes this message an emergency, or None."""
    if not text:
        return None
    said = _said(text)
    return next((p for p in phrases(business) if p in said), None)


def clean_phone(raw: str | None) -> str | None:
    """The business's own emergency number as typed, if it looks like one."""
    value = " ".join(str(raw or "").split())[:PHONE_MAX]
    return value if value and _PHONE.match(value) else None


def reply(business, text: str | None) -> str:
    """Where to get help, in the language they wrote in, with the business's own emergency number if it set one."""
    said = _said(text or "")
    # The words that matched say it best ("behosh" is Hinglish however short the message); else the message's language.
    key = next((group for group, words in _set(business).items() if any(w in said for w in words)), None)
    if key is None:
        language = lang.detect(text) if text else None
        key = language if language in _REPLY else "english"
    line = _REPLY[key]
    number = clean_phone((getattr(business, "settings", None) or {}).get("emergency_phone"))
    if number:
        line += _OWN_NUMBER[key].format(number=number)
    return line


def reason(phrase: str) -> str:
    """What the team sees: on the Escalations page and in the alert."""
    return f"EMERGENCY - customer said \"{phrase.strip()}\""
