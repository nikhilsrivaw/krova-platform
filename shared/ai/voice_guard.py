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

_CLAIM = re.compile(
    r"\b(khatam|khatm|available|confirm(?:ed)?|booked|cancel(?:led|ed)?|paid|payment|slot|"
    r"am|pm)\b",
    re.IGNORECASE,
)


def ungrounded_claims(reply: str, business_facts: str) -> list[str]:
    facts = business_facts.lower()
    return sorted({
        word.lower() for word in _CLAIM.findall(reply)
        if not re.search(rf"\b{re.escape(word.lower())}\b", facts)
    })
