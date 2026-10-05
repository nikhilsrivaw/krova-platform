"""
Reads a Justdial lead payload into plain fields.

Not verified against a live Justdial payload yet. The key names below are the
common spellings for lead platforms, not Justdial's documented schema. A lead
whose name or phone is missed shows up in inbound_leads.raw_payload, so the
fix is one more key in _FIELDS, not a resend from Justdial.

Kept free of database imports so it can be tested on its own.
"""

from dataclasses import dataclass

_FIELDS: dict[str, tuple[str, ...]] = {
    "name": ("name", "customer_name", "full_name", "Name"),
    "phone": ("mobile", "phone", "mobile_number", "contact", "Mobile", "Phone"),
    "email": ("email", "email_id", "Email"),
    "query": ("query", "requirement", "enquiry", "message", "comments", "Query"),
    "external_id": ("lead_id", "enquiry_id", "LeadId", "id"),
}


@dataclass(slots=True)
class ParsedLead:
    name: str | None
    phone: str | None
    email: str | None
    query: str | None
    external_id: str | None


def _pick(source: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = source.get(key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return None


def parse_lead(payload: dict) -> ParsedLead:
    # Some platforms wrap the lead in a "data" object - read from there if so.
    source = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    return ParsedLead(**{field: _pick(source, keys) for field, keys in _FIELDS.items()})
