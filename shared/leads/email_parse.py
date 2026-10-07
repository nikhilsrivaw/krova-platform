"""
Reads a raw forwarded email (the exact bytes Postfix hands to the pipe
script) into the shared lead shape.

No portal's real alert-email layout has been seen yet, so this works off two
things that do not depend on any one layout:
1. Labelled lines ("Name: ...", "Mobile: ...") - the same aliases the other
   lead parsers use, checked line by line instead of by a dict key.
2. A bare 10-digit Indian mobile number pattern, as a fallback when nothing
   is labelled - most portal emails have the number somewhere even when the
   rest of the layout varies.

The full raw text is always kept on the InboundLead row, so a labelling miss
is fixed by adding one alias, not by resending anything.
"""

import re
from dataclasses import dataclass
from email import message_from_bytes
from email.message import Message

from shared.leads.justdial_parse import ParsedLead, _FIELDS

_LABEL_PATTERNS = {
    field: [re.compile(rf"^\s*{re.escape(alias)}\s*[:\-]\s*(.+?)\s*$", re.IGNORECASE)
            for alias in aliases]
    for field, aliases in _FIELDS.items()
}
_PHONE_FALLBACK = re.compile(r"\b([6-9]\d{9})\b")


@dataclass(slots=True)
class RawEmail:
    to_address: str | None
    from_address: str | None
    subject: str | None
    body_text: str
    message_id: str | None


def parse_raw_email(raw_bytes: bytes) -> RawEmail:
    msg: Message = message_from_bytes(raw_bytes)
    return RawEmail(
        to_address=msg.get("To"),
        from_address=msg.get("From"),
        subject=msg.get("Subject"),
        body_text=_extract_text(msg),
        message_id=msg.get("Message-ID"),
    )


def _extract_text(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return _decode(part)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return re.sub(r"<[^>]+>", " ", _decode(part))
        return ""
    return _decode(msg)


def _decode(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return str(part.get_payload())
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def recipient_local_part(to_address: str | None) -> str | None:
    """The "leads-<token>" part of "leads-<token>@leads.krova.space", or None."""
    if not to_address:
        return None
    match = re.search(r"<?([^<>\s@]+)@[^<>\s@]+>?", to_address)
    return match.group(1) if match else None


def extract_lead_from_body(body: str) -> ParsedLead:
    values: dict[str, str | None] = {"name": None, "phone": None, "email": None, "query": None}
    for line in body.splitlines():
        for field, patterns in _LABEL_PATTERNS.items():
            if field == "external_id" or values.get(field):
                continue
            for pattern in patterns:
                m = pattern.match(line)
                if m:
                    values[field] = m.group(1).strip()
                    break
    if not values["phone"]:
        m = _PHONE_FALLBACK.search(body)
        if m:
            values["phone"] = m.group(1)
    return ParsedLead(
        name=values["name"], phone=values["phone"], email=values["email"],
        query=values["query"], external_id=None,
    )
