"""
The words a business uses for a shared mechanism.

The queue that a clinic calls "a token for a patient" a restaurant calls "a
table for a guest" and a passport office calls "a number for an applicant". It
is the same waiting line either way, so it is the same code either way - only
the vocabulary differs, and vocabulary belongs in data, not in Python.

Three tiers, each overriding the one before:

1. The code default, deliberately neutral - correct for a business nobody has
   thought about yet, which is most of them.
2. The vertical template's own words, so a clinic reads like a clinic on day
   one without anybody configuring anything.
3. The business's own words, which beat both. This is the tier that makes a
   new kind of business a settings change rather than a code change.
"""

from typing import TYPE_CHECKING

from shared.verticals import get

if TYPE_CHECKING:
    from shared.db.models import Business

_QUEUE_DEFAULTS: dict = {
    "person": "customer",
    "ticket": "number",
    "serving": "Being served",
    "shifts": {
        "morning": "Morning",
        "evening": "Evening",
        # The third track is not "an emergency" outside a clinic - it is
        # whatever jumps the line. "Priority" is the neutral name for it.
        "emergency": "Priority",
    },
}

_SCHEDULING_DEFAULTS: dict = {
    "provider": "Provider",
    "provider_plural": "Providers",
    "credential_label": "Details",
    "fee_label": "Fee",
    "booking_noun": "booking",
    "booking_noun_plural": "bookings",
}


def _merge(base: dict, overlay: object) -> dict:
    """
    Overlay onto base, one nesting level deep, keeping base's exact shape.

    Keys base doesn't declare are dropped rather than passed through: these
    dicts are rendered straight into a UI and into an agent prompt, so the
    set of keys is a contract, not whatever happens to be in a JSONB bag.
    A blank value is also dropped, which is what makes clearing a field in
    Settings fall back to the default instead of rendering an empty string.
    """
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    if not isinstance(overlay, dict):
        return out
    for key, value in overlay.items():
        if key not in out:
            continue
        if isinstance(out[key], dict):
            out[key] = _merge(out[key], value)
        elif isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


def queue_labels(business: "Business") -> dict:
    """What this business calls the parts of its queue."""
    template = get(business.vertical).get("labels", {}).get("queue")
    own = ((business.settings or {}).get("queue") or {}).get("labels")
    return _merge(_merge(_QUEUE_DEFAULTS, template), own)


def scheduling_labels(business: "Business") -> dict:
    """
    What this business calls the parts of its scheduling - a clinic's
    "Doctor" and a real estate agency's "Agent" are the same underlying
    row (a person with recurring hours a customer books against), so this
    is vocabulary, not two different mechanisms. A vertical need not
    declare every key - real_estate omits fee_label since an agent's row
    rarely has one, and it falls through to the neutral default below.
    """
    template = get(business.vertical).get("labels", {}).get("scheduling")
    own = ((business.settings or {}).get("scheduling") or {}).get("labels")
    return _merge(_merge(_SCHEDULING_DEFAULTS, template), own)
