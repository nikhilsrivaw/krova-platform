"""
Vertical templates - "a clinic in a box".

Every competitor ships an empty flow builder and calls it flexibility. A tier-2
SMB never fills it in, so the product is dead on arrival. Choosing a vertical
at signup instead means the agent knows how a clinic speaks, what a clinic gets
asked, and what a clinic must never answer - before a single conversation
exists.

These are JSON files on purpose. Adding "salon" must be writing a file, not
touching Python. Get that boundary wrong and every vertical becomes a fork of
the codebase, and by the fourth one you stop shipping.

Two deliberately, plus a general fallback. A shallow template is worse than
none: it makes the product feel wrong in the first thirty seconds. Each new
vertical gets added when it has been researched properly, not to lengthen a
list on a pricing page.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shared.db.models import Business

_TEMPLATE_DIR = Path(__file__).parent / "templates"

FALLBACK = "general"


class UnknownVertical(ValueError):
    pass


@lru_cache
def _load_all() -> dict[str, dict]:
    templates: dict[str, dict] = {}
    for path in sorted(_TEMPLATE_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        key = data.get("key") or path.stem
        if key != path.stem:
            raise ValueError(
                f"vertical template {path.name} declares key {key!r}; "
                "filename and key must match"
            )
        templates[key] = data
    if FALLBACK not in templates:
        raise RuntimeError(f"the {FALLBACK!r} vertical template is required")
    return templates


def available() -> list[dict[str, str]]:
    """
    Every vertical a business can choose, for the signup screen.

    Filters out templates marked `"selectable": false`. Onboarding asks for
    a business's *market type* - the shape of how it sells - rather than its
    industry, because what decides whether this platform is useful is
    whether there is a gap between the conversation and the money, not what
    the business happens to sell. A salon and a restaurant pick the same
    thing; a clinic, where the patient pays at the desk, is out of scope
    entirely.

    The old industry templates (clinic, restaurant, ecommerce, ...) stay on
    disk rather than being deleted, because `get()` raises on an unknown key
    rather than falling back - deleting one would break every business
    already on it. They are hidden here, still loadable, and migrated
    deliberately rather than by removal.
    """
    return [
        {"key": t["key"], "label": t["label"], "summary": t["summary"]}
        for t in sorted(_load_all().values(), key=lambda t: t["key"] != FALLBACK)
        if t.get("selectable", True)
    ]


def keys() -> set[str]:
    return set(_load_all())


def get(key: str) -> dict:
    """
    Load one template.

    Raises rather than falling back silently: a business quietly seeded with
    the wrong vertical would behave subtly wrongly forever, and nobody would
    know to look here.
    """
    templates = _load_all()
    if key not in templates:
        raise UnknownVertical(
            f"Unknown vertical {key!r}. Available: {', '.join(sorted(templates))}"
        )
    return templates[key]


def capabilities_for(business: "Business") -> list[str]:
    """
    What this business can actually use: its vertical's declared list, then
    its own on/off overrides from settings["capability_overrides"].

    The vertical is where a business starts, not what it is forever. A
    restaurant running a walk-in waitlist needs exactly the mechanism a
    clinic's OPD queue already is; the alternative is a near-identical
    capability per vertical, and by the fourth one every vertical is a fork
    of the codebase - the failure this module's own docstring exists to
    prevent. So the template answers "what should this kind of business have
    by default" and the business answers "what do I actually use".
    """
    caps = list(get(business.vertical).get("capabilities", []))
    overrides = (business.settings or {}).get("capability_overrides") or {}
    for capability, enabled in overrides.items():
        if enabled and capability not in caps:
            caps.append(capability)
        elif not enabled and capability in caps:
            caps.remove(capability)
    return caps


def has_capability(business: "Business", capability: str) -> bool:
    """
    Whether this business gets a given capability - Scheduling, Voice
    Booking, and so on.

    The check, not the capability itself: the module implementing a
    capability (shared/scheduling, for instance) is written once and shared
    by every business that has it, per the project's standing rule against
    per-vertical subclassing. This function is how a caller (a WhatsApp reply
    handler, a voice turn) asks "does this business get to use this" without
    knowing or caring which vertical it is.
    """
    return capability in capabilities_for(business)


def seed_dna(key: str) -> dict:
    """
    The BusinessDNA field values a new business starts with.

    Only the parts a template can honestly know - how this kind of business
    speaks, what it must not answer, what tends to get promised. The specifics
    (actual prices, actual hours) stay empty until the owner supplies them or
    the conversations reveal them. Inventing those would be inventing facts
    about someone's business.
    """
    template = get(key)
    return {
        "summary": template["summary"],
        "tone": template["tone"],
        "policies": template["policies"],
        "known_gaps": {
            "from_template": template.get("known_gaps", []),
            "learned": [],
        },
        "offerings": {},
        "opening_hours": {},
        "pricing_notes": None,
        "source": "template",
    }
