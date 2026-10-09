"""
What a carousel needs before it can be sent - kept apart from
shared/channels/carousel_send.py (which sends) so the AI's context builder can
ask the same questions without importing every channel client.

A carousel can carry {{variables}} - a name in the intro, a price in a card.
Three different callers meet them differently:

  an AI reply        fills them in from the conversation (the customer's
                     name, a number they wrote) - see share_carousel_values
                     in shared/ai/agent.py;
  an automation rule has nothing to fill them with, so it only sends a
                     carousel that needs nothing;
  a person           sends by hand and sees what they send.

So this module describes the slots, and checks that the values supplied fill
every one. A carousel is never sent with a slot left blank - Meta would
deliver the literal "{{1}}" to the customer.
"""

import re
from dataclasses import dataclass, field

from shared.channels.whatsapp import templates as wa_templates
from shared.db.models import TemplateStatus

# Meta rejects newlines and tabs inside a template parameter, and a runaway
# value is almost certainly the model rambling rather than a name or a number.
MAX_VALUE_LENGTH = 200

_VARIABLE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


@dataclass(slots=True)
class CardSpec:
    text: str
    variables: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WhatsAppCarouselSpec:
    media_ids: list[str]
    body_text: str
    body_variables: list[str]
    cards: list[CardSpec]

    @property
    def needs_values(self) -> bool:
        return bool(self.body_variables) or any(c.variables for c in self.cards)


def whatsapp_carousel_spec(template) -> WhatsAppCarouselSpec | None:
    """
    The shape of an approved WhatsApp carousel template, or None if it is not
    one that can be sent at all: not approved, not really a carousel, or
    without a stored image for every card.
    """
    if template is None or template.status != TemplateStatus.approved:
        return None
    components = template.components if isinstance(template.components, list) else []
    carousel = next((c for c in components if isinstance(c, dict) and c.get("type") == "CAROUSEL"), None)
    raw_cards = (carousel or {}).get("cards") or []
    media_ids = list((template.extra or {}).get("carousel_media_ids") or [])
    if not raw_cards or len(media_ids) != len(raw_cards):
        return None

    cards = []
    for card in raw_cards:
        text = next((c.get("text", "") for c in card.get("components", []) if c.get("type") == "BODY"), "")
        cards.append(CardSpec(text=text, variables=wa_templates.variables_in(text)))
    return WhatsAppCarouselSpec(
        media_ids=media_ids,
        body_text=template.body_text or "",
        body_variables=wa_templates.variables_in(template.body_text or ""),
        cards=cards,
    )


def usable_whatsapp_carousel_media(template) -> list[str] | None:
    """
    The media ids of a carousel that needs NO values - sendable by a caller
    with nothing to fill it in (an automation rule). None if it is not
    sendable, or if it has any {{variable}}.
    """
    spec = whatsapp_carousel_spec(template)
    if spec is None or spec.needs_values:
        return None
    return spec.media_ids


def instagram_element_slots(element: dict) -> list[str]:
    """The {{placeholders}} in one saved Instagram card's title and subtitle, in order."""
    return wa_templates.variables_in(f"{element.get('title') or ''}\n{element.get('subtitle') or ''}")


def instagram_carousel_needs_values(elements: list[dict] | None) -> bool:
    return any(instagram_element_slots(e) for e in (elements or []))


def fill(text: str | None, names: list[str], values: list[str]) -> str | None:
    """Replace each {{name}} in `text` with the value at the same position in `names`."""
    if text is None:
        return None
    mapping = dict(zip(names, values))
    return _VARIABLE.sub(lambda m: mapping.get(m.group(1), m.group(0)), text)


def clean_value(value) -> str:
    """One parameter, safe to hand to Meta/Instagram: a single line, trimmed, bounded."""
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()[:MAX_VALUE_LENGTH]


def clean_values(raw) -> dict | None:
    """
    The AI's share_carousel_values reduced to {"body": [str], "cards": [[str]]},
    or None if there is nothing usable. Shape only - whether the values cover
    every slot is checked against the specific carousel at send time.
    """
    if not isinstance(raw, dict):
        return None
    body = raw.get("body") if isinstance(raw.get("body"), list) else []
    cards = raw.get("cards") if isinstance(raw.get("cards"), list) else []
    cleaned = {
        "body": [clean_value(v) for v in body],
        "cards": [
            [clean_value(v) for v in card] if isinstance(card, list) else []
            for card in cards
        ],
    }
    if not cleaned["body"] and not any(cleaned["cards"]):
        return None
    return cleaned


def covers(slots: list[str], supplied: list[str] | None) -> bool:
    """
    True if `supplied` gives exactly one non-empty value for each slot. No
    slots means nothing to fill, so anything supplied is simply ignored.
    """
    if not slots:
        return True
    supplied = supplied or []
    return len(supplied) == len(slots) and all(v for v in supplied)


def describe_slots(spec: WhatsAppCarouselSpec) -> str:
    """What the AI must fill in, with the text around each blank, e.g. `intro "Hi {{1}}" ; card 2 "Only {{1}}"`."""
    parts = []
    if spec.body_variables:
        parts.append(f'intro "{spec.body_text.strip()[:120]}"')
    for index, card in enumerate(spec.cards, start=1):
        if card.variables:
            parts.append(f'card {index} "{card.text.strip()[:100]}"')
    return " ; ".join(parts)


def describe_instagram_slots(elements: list[dict] | None) -> str:
    parts = []
    for index, element in enumerate(elements or [], start=1):
        if instagram_element_slots(element):
            text = " / ".join(t for t in (element.get("title"), element.get("subtitle")) if t)
            parts.append(f'card {index} "{text[:120]}"')
    return " ; ".join(parts)
