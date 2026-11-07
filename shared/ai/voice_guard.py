"""
Stops a voice reply from stating a fact the business never gave us.

The failures seen so far are a small, fixed set: a slot or appointment called
free or booked, a payment or order called received, a time called taken. Each
is a claim word. A reply that uses a claim word is only allowed when the same
word appears in what the business itself said - its details or its own earlier
messages. The customer's words never count, because a customer asking "confirm
hai na?" does not make it confirmed.
"""

import re
import unicodedata

_CLAIM = re.compile(
    r"\b(khatam|khatm|available|confirm(?:ed)?|booked|cancel(?:led|ed)?|paid|payment|slot|"
    r"am|pm|appointment|booking|connect|shortly|follow|band|hata|rad|badal|meeting|arrange|confirmation|reschedul|change|"
    r"sent|bhej diya|bhej di|bhej dee|bheja gaya|bhej raha|bhej rahe|kar diya gaya)\b",
    re.IGNORECASE,
)


_RECORD_CLAIM = re.compile(
    r"\b(meeting|appointment|booking|booked|cancel\w*|reschedul\w*|confirm\w*|paid|payment)\b"
    r"|naam pe|ke naam",
    re.IGNORECASE,
)


def has_record_claim(text: str) -> bool:
    """Text that talks about a meeting, booking, payment or a named person's record."""
    return bool(_RECORD_CLAIM.search(text or ""))


_FOREIGN_SCRIPT = re.compile(r"[؀-ۿЀ-ӿ぀-ヿ一-鿿]")


def has_foreign_script(text: str) -> bool:
    """Arabic, Cyrillic, Japanese or Chinese characters in a reply meant for an Indian caller."""
    return bool(_FOREIGN_SCRIPT.search(text or ""))


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> list[float]:
    """Every number in a text, read in any Indian script's digits."""
    plain = "".join(str(unicodedata.digit(ch)) if ch.isdigit() and unicodedata.digit(ch, None) is not None else ch for ch in text or "")
    found = []
    for raw in _NUMBER.findall(plain):
        try:
            found.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return found


def ungrounded_numbers(reply: str, facts: str) -> list[float]:
    """
    Numbers in a reply that nothing on file says: "bus number 25", "15 minutes by auto", a total the model added up. Works
    in every script, where the claim words above cannot. Under 10 is left alone ("2 saal", "ek ya 2 din"). "40 hazaar" for
    40,000 is stopped too: allowing it let an invented "bus number 18" through on a fee of 18,000, and a person following
    up costs less than a wrong number. Tested replies wrote fees out in full.
    """
    known = set(_numbers(facts))
    return [n for n in _numbers(reply) if n >= 10 and n not in known]


def ungrounded_claims(reply: str, business_facts: str) -> list[str]:
    facts = business_facts.lower()
    return sorted({
        word.lower() for word in _CLAIM.findall(reply)
        if not re.search(rf"\b{re.escape(word.lower())}\b", facts)
    })
