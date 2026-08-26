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
    """Every vertical a business can choose, for the signup screen."""
    return [
        {"key": t["key"], "label": t["label"], "summary": t["summary"]}
        for t in sorted(_load_all().values(), key=lambda t: t["key"] != FALLBACK)
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


def has_capability(key: str, capability: str) -> bool:
    """
    Whether this vertical declares a given capability - Scheduling, Voice
    Booking, and so on.

    The check, not the capability itself: the module implementing a
    capability (shared/scheduling, for instance) is written once and shared
    by every vertical that declares it, per the project's standing rule
    against per-vertical subclassing. This function is how a caller (a
    WhatsApp reply handler, a voice turn) asks "does this business get to
    use this" without knowing or caring which vertical it is.
    """
    return capability in get(key).get("capabilities", [])


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
