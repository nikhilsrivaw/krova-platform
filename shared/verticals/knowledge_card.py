"""
Knowledge as two short cards, the same for every business type: the Business Card and the Team Card.

Why short: the agent reads all of it before every reply, so every character is paid for on every message. The cards
hold only what customers ask about again and again (docs/new/knowledge-card-design.md: 65% of the common questions in
every template are answered by these fields; the rest belong to a customer's own record or to the owner). Anything
long goes behind a link (`full_list`); anything decided case by case goes to the owner.

The fields never change by business type - one screen, one code path. A type may only reword them (title, question,
placeholder) with a `knowledge_card` block in its template:

    "knowledge_card": {"offerings": {"title": "Top plans and fees", "question": "...", "placeholder": "..."}}

Each field is saved as one KnowledgeItem with `topic` = the field key; the Team Card is one item with topic "team"
(its rows kept in `extra` so they can be edited); Top questions are separate short FAQ items, at most FAQ_LIMIT.
"""

import re
from typing import Any

from shared.verticals import UnknownVertical, get

FAQ_LIMIT = 10
FAQ_QUESTION_LIMIT = 150
FAQ_ANSWER_LIMIT = 250

# "Other entries": free notes for what fits no card field. Kept small on purpose - the AI reads them on every message.
OTHER_ITEMS_LIMIT = 5
OTHER_TOKENS_LIMIT = 1000
OTHER_CHARS_LIMIT = OTHER_TOKENS_LIMIT * 4

TEAM_TOPIC = "team"
TEAM_PEOPLE_LIMIT = 10
TEAM_LIMITS = {"name": 40, "role": 60, "handles": 150, "languages": 60}
TEAM_TEXT_LIMITS = {"contact": 250, "languages": 100, "response_note": 200}

# Ordered as the owner sees them. `limit` is in characters; about 4 characters make a token.
CARD_FIELDS: list[dict[str, Any]] = [
    {"key": "about", "title": "What you do", "kind": "service", "limit": 300, "required": True,
     "question": "What does your business do, and who is it for? Two lines.",
     "placeholder": "We are a ... in ... . We help ... with ... ."},
    {"key": "where", "title": "Where", "kind": "other", "limit": 400, "required": True,
     "question": "Address of each branch with a landmark, and the areas you serve or deliver to.",
     "placeholder": "... Road, near ..., City. We serve ... and deliver to ..."},
    {"key": "when", "title": "Opening hours", "kind": "hours", "limit": 300, "required": True,
     "question": "When are you open? Each day and branch, and closed days.",
     "placeholder": "Mon-Sat ... to ..., Sunday ... ."},
    {"key": "offerings", "title": "Top offerings and prices", "kind": "price_list", "limit": 1000, "required": True,
     "question": "The things customers ask about most, up to 10 lines, each with its price and unit (or a price range).",
     "placeholder": "... - Rs ... per ... \n... - Rs ... to Rs ..."},
    {"key": "full_list", "title": "Full list link", "kind": "other", "limit": 300, "required": False, "link": True,
     "question": "A link to your full price list, menu, catalogue or brochure. The AI sends this link instead of guessing.",
     "placeholder": "https://..."},
    {"key": "how_to_buy", "title": "How to buy or book", "kind": "service", "limit": 400, "required": True,
     "question": "How does a customer buy, order or book? Walk-in or appointment, trial or demo, minimum order, documents needed.",
     "placeholder": "Book on WhatsApp or walk in. Free ... on request. Minimum order ... ."},
    {"key": "payment", "title": "Payment", "kind": "policy", "limit": 300, "required": True,
     "question": "How can customers pay? Advance or deposit, instalments, EMI or credit.",
     "placeholder": "UPI, card and cash. ...% advance; balance on ... ."},
    {"key": "timelines", "title": "How long things take", "kind": "service", "limit": 300, "required": False,
     "question": "How long do things take: dispatch, delivery, a service, a waiting period?",
     "placeholder": "Delivery in ... days. A ... takes about ... ."},
    {"key": "changes", "title": "Cancel, return and refund", "kind": "policy", "limit": 400, "required": True,
     "question": "Your rule for cancelling, returning, pausing, missing a visit and refunds.",
     "placeholder": "Cancel up to ... before for a full refund. Returns within ... days if ... ."},
    {"key": "trust", "title": "Why trust us, and features", "kind": "other", "limit": 300, "required": False,
     "question": "Years in business, licences or registrations, past results stated as facts, and features (parking, veg, home visit...).",
     "placeholder": "Since ..., registered ..., ... . Parking available."},
    {"key": "offers", "title": "Offers and notices", "kind": "other", "limit": 300, "required": False, "expires": True,
     "question": "Anything running right now: an offer, a price change, a holiday closure. Set the last day.",
     "placeholder": "... offer until ... . Closed on ... ."},
]

FIELD_KEYS = {f["key"] for f in CARD_FIELDS}

# Words that suggest which field a gap belongs to (English and Hinglish). A suggestion only: the owner chooses.
GAP_HINTS: dict[str, tuple[str, ...]] = {
    "when": ("timing", "timings", "open", "close", "closed", "hours", "khula", "khule", "sunday", "holiday", "baje"),
    "where": ("address", "location", "where", "kahan", "kaha", "deliver", "area", "pincode", "branch", "city", "parking"),
    "offerings": ("price", "cost", "rate", "fee", "fees", "kitna", "kitne", "charge", "plan", "menu", "model", "product", "service"),
    "how_to_buy": ("book", "booking", "appointment", "order", "trial", "demo", "minimum", "moq", "document", "admission", "test drive"),
    "payment": ("pay", "payment", "upi", "card", "cash", "emi", "instalment", "installment", "loan", "credit", "advance", "insurance"),
    "timelines": ("how long", "kitne din", "delivery time", "dispatch", "waiting", "ready", "kitna time", "time lagega"),
    "changes": ("cancel", "refund", "return", "exchange", "pause", "freeze", "reschedule", "wapas", "vapas"),
    "trust": ("certified", "registered", "experience", "years", "licence", "license", "rera", "iso", "genuine", "original", "veg"),
    "offers": ("offer", "sale", "festival", "deal", "coupon", "scheme"),
    TEAM_TOPIC: ("call", "phone", "number", "contact", "speak", "talk", "doctor", "trainer", "staff", "who", "manager", "email"),
}

_URL = re.compile(r"^https?://\S+$")


def fields_for(vertical: str) -> list[dict[str, Any]]:
    """The card fields, with this business type's wording. Never adds or removes a field."""
    try:
        wording = get(vertical).get("knowledge_card") or {}
    except UnknownVertical:
        wording = {}
    out = []
    for field in CARD_FIELDS:
        f = dict(field)
        for attr in ("title", "question", "placeholder"):
            if attr in wording.get(f["key"], {}):
                f[attr] = wording[f["key"]][attr]
        f["required"] = bool(f.get("required"))
        f["expires"] = bool(f.get("expires"))
        f["link"] = bool(f.get("link"))
        out.append(f)
    return out


def is_other(item: Any) -> bool:
    """A free note: not a card field, not the Team Card, not a top question."""
    return not getattr(item, "topic", None) and (getattr(item, "extra", None) or {}).get("card") != "faq"


def check_other_room(active_others: list[tuple[Any, int]], new_tokens: int) -> str | None:
    """
    A reason one more (or a bigger) free note cannot be switched on, or None.
    `active_others` is (id, tokens) for every active free note except the one being saved.
    """
    if len(active_others) + 1 > OTHER_ITEMS_LIMIT:
        return (
            f"You can keep {OTHER_ITEMS_LIMIT} other entries switched on. Put this on the Business card, "
            "or switch an entry off first - the AI reads every entry on every message."
        )
    used = sum(t for _, t in active_others)
    if used + new_tokens > OTHER_TOKENS_LIMIT:
        return (
            f"Other entries are kept to about {OTHER_TOKENS_LIMIT} tokens in total ({OTHER_CHARS_LIMIT} characters); "
            f"{used} are used. Shorten this, or put it on the Business card."
        )
    return None


def check_value(field: dict[str, Any], content: str) -> str | None:
    """A reason this answer cannot be saved, or None."""
    if len(content) > field["limit"]:
        return f"Keep '{field['title']}' to {field['limit']} characters - the AI reads it on every message."
    if field.get("link") and content and not _URL.match(content):
        return "Paste one full link starting with http:// or https://"
    return None


def suggest_field(gap: str) -> str | None:
    """The field a gap most likely belongs to, by word hits; None when nothing clearly matches."""
    text = (gap or "").lower()
    words_in_text = set(re.findall(r"[a-z]+", text))

    def hit(hint: str) -> bool:
        # a phrase matches anywhere; a single word only as a whole word ("card" must not match "discard")
        return hint in text if " " in hint else hint in words_in_text

    scores = {key: sum(1 for w in words if hit(w)) for key, words in GAP_HINTS.items()}
    best = max(scores.values(), default=0)
    if best == 0:
        return None
    winners = [k for k, v in scores.items() if v == best]
    return winners[0] if len(winners) == 1 else None


def render_team(card: dict[str, Any]) -> str:
    """The Team Card as the text the agent reads. Empty parts are left out."""
    lines: list[str] = []
    if card.get("contact"):
        lines.append(f"How to reach us: {card['contact']}")
    people = [p for p in card.get("people", []) if p.get("name")]
    if people:
        lines.append("Who customers can ask for:")
        for p in people:
            bits = [p["name"]]
            if p.get("role"):
                bits.append(p["role"])
            line = ", ".join(bits)
            if p.get("handles"):
                line += f" - {p['handles']}"
            if p.get("languages"):
                line += f" (speaks {p['languages']})"
            lines.append(f"- {line}")
    if card.get("languages"):
        lines.append(f"Languages we reply in: {card['languages']}")
    if card.get("response_note"):
        lines.append(f"When we reply: {card['response_note']}")
    return "\n".join(lines)


def coverage(fields: list[dict[str, Any]], answered: set[str], has_team: bool, faqs: int) -> dict[str, int]:
    required = [f["key"] for f in fields if f["required"]]
    return {
        "required_total": len(required),
        "required_done": sum(1 for k in required if k in answered),
        "fields_total": len(fields),
        "fields_done": sum(1 for f in fields if f["key"] in answered),
        "team_done": int(has_team),
        "faqs": faqs,
        "faq_limit": FAQ_LIMIT,
    }
