"""
Reads an IndiaMART CRM Push payload into the shared lead shape.

Field names are IndiaMART's own, from its CRM Push API spec
(help.indiamart.com, "Integration of IndiaMART's Lead Manager CRM Push API").
The payload is JSON, POST, and the URL itself is the only identifier.
"""

from shared.leads.justdial_parse import ParsedLead


def parse_indiamart(payload: dict) -> ParsedLead:
    def pick(*keys: str) -> str | None:
        for key in keys:
            value = payload.get(key)
            if value is not None and str(value).strip() != "":
                return str(value).strip()
        return None

    product = pick("QUERY_PRODUCT_NAME")
    message = pick("QUERY_MESSAGE") or pick("SUBJECT")
    city = pick("SENDER_CITY")
    parts = [p for p in (product, message) if p]
    if city:
        parts.append(f"city: {city}")
    query = " | ".join(parts) or None

    return ParsedLead(
        name=pick("SENDER_NAME"),
        phone=pick("SENDER_MOBILE", "SENDER_PHONE", "SENDER_MOBILE_ALT", "SENDER_PHONE_ALT"),
        email=pick("SENDER_EMAIL", "SENDER_EMAIL_ALT"),
        query=query,
        external_id=pick("UNIQUE_QUERY_ID"),
    )
