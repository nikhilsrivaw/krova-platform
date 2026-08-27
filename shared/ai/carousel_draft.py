"""
Drafting carousel card copy - a starting point a human edits, never a
finished template a human just approves.

The images are a business's own photos and never invented here. What this
writes is the two lines per card and a button label, from a short brief the
business gives ("a retention offer for customers who haven't ordered in a
while") plus their own business context - the same DNA every other prompt
in this platform already reads from. A human reviews and edits every card
before anything is submitted to Meta, the same discipline as every other
AI-authored thing on this platform: drafted, never sent unreviewed.
"""

from dataclasses import dataclass

from shared.ai import client
from shared.utils.logging import get_logger

logger = get_logger(__name__)

MIN_CARDS = 2
MAX_CARDS = 10

DRAFT_TOOL = {
    "name": "record_cards",
    "description": "Draft the text for each carousel card.",
    "input_schema": {
        "type": "object",
        "properties": {
            "cards": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "body": {
                            "type": "string",
                            "description": (
                                "Up to 160 characters. What this specific card says - "
                                "one idea per card, not a repeat of the others."
                            ),
                        },
                        "button_label": {
                            "type": "string",
                            "description": "Up to 25 characters. A short call to action for this card.",
                        },
                    },
                    "required": ["body", "button_label"],
                },
            }
        },
        "required": ["cards"],
    },
}

SYSTEM = """You draft WhatsApp carousel template card text for a business.

A carousel is a horizontally-scrolling set of cards inside one message, each
with its own picture, two lines of text and a button. You write the text
only - the pictures are the business's own, chosen separately.

Rules:
- Each card's body is under 160 characters and says ONE thing plainly. No
  card should repeat what another card already says.
- If the brief implies real facts (a price, a date, a name), use a
  {{variable_name}} placeholder rather than inventing a number - a template
  variable gets filled per-recipient later, a fabricated one does not.
- Button labels are under 25 characters, in the imperative: "Book Now",
  "View Offer", not "You can book here".
- Write in the voice the business context describes. If none is given,
  write plainly and directly - no exclamation-mark marketing voice.

This is a draft a human will read and edit before anything is sent to Meta
for review. Write something worth editing, not something so generic it has
to be rewritten from scratch."""


@dataclass(slots=True)
class DraftCard:
    body: str
    button_label: str


@dataclass(slots=True)
class CardDraft:
    cards: list[DraftCard]
    cost_paise: int


async def draft(
    *, brief: str, card_count: int, business_context: str,
) -> CardDraft:
    """Propose card_count cards' worth of text for a human to edit."""
    card_count = max(MIN_CARDS, min(MAX_CARDS, card_count))

    prompt = (
        f"Business: {business_context}\n\n"
        f"What this carousel is for: {brief}\n\n"
        f"Draft exactly {card_count} cards."
    )

    completion = await client.complete(
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        speed="fast",
        tool=DRAFT_TOOL,
        max_tokens=1024,
    )

    raw = (completion.tool_input or {}).get("cards")
    cards = [
        DraftCard(
            body=(item.get("body") or "").strip()[:160],
            button_label=(item.get("button_label") or "").strip()[:25],
        )
        for item in (raw if isinstance(raw, list) else [])
        if isinstance(item, dict) and (item.get("body") or "").strip()
    ]

    if not cards:
        logger.warning("carousel draft returned no usable cards")

    return CardDraft(cards=cards, cost_paise=completion.cost_paise)
