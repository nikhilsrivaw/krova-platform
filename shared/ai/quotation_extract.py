"""
Reading a quotation out of a conversation.

The point of the whole Type 1 build: a B2B rep sends a quote on WhatsApp
("50 pcs @ 499, dispatch Tuesday") and it exists nowhere else. Every CRM
in this market asks them to type it in again, which is why 55% of CRM
rollouts fail on adoption. This reads it instead.

**Built to be safe while it is still unvalidated.** Nobody has tuned this
against real Indian B2B chat yet, and that chat is genuinely hard - Hindi
and English mixed mid-sentence, amounts as "2.5L" or "₹1.2 lakh",
quantities in pieces/cartons/lots, and prices quoted per-unit or in total
with no marker of which. So rather than guess well, this is built so that
guessing badly is harmless:

- Extracted quotes land as `draft`, never `sent`. A draft is a suggestion
  a human confirms, so a wrong one costs a dismissal rather than a wrong
  follow-up sent to a buyer.
- A confidence floor, below which nothing is written at all.
- Every quote cites the message ids it came from, and keeps the verbatim
  wording, so a person can check the reading rather than trust it. Same
  provenance discipline as shared/ai/commitments.py.
- Amounts that cannot be read confidently are left null rather than
  invented. A quote with no total is still useful; a quote with a wrong
  total is worse than none.

When real conversations exist, the thing to tune is the threshold and the
examples in SYSTEM - not the safety, which should stay.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from shared.ai import client
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Below this, nothing is written. Deliberately higher than the commitment
# extractor's own 0.75: a spurious commitment is a line in a ledger, while
# a spurious quotation is a number someone may quote back to a customer.
CONFIRM_THRESHOLD = 0.8

EXTRACT_TOOL = {
    "name": "record_quotation",
    "description": (
        "Record a price quotation the business gave the customer in this "
        "conversation. Return nothing if the business did not actually quote "
        "a price - most conversations contain no quotation, and that is the "
        "correct answer. A customer asking 'what is the rate' is not a "
        "quotation; only the business's own answer is."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "found": {
                "type": "boolean",
                "description": "True only if the business actually quoted a price or terms.",
            },
            "total_amount": {
                "type": ["string", "null"],
                "description": (
                    "The total quoted, exactly as written in the message "
                    "('2.5L', '₹1,25,000', '499 per piece'). Null if the "
                    "business gave no number. Never calculate or infer one."
                ),
            },
            "reference": {
                "type": ["string", "null"],
                "description": "A quote number if one was mentioned, else null.",
            },
            "items": {
                "type": "array",
                "description": "The things quoted. Empty if not itemised.",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "quantity": {
                            "type": ["string", "null"],
                            "description": "As written, with the unit: '50 pcs', '2 cartons'.",
                        },
                        "unit_price": {
                            "type": ["string", "null"],
                            "description": "Per-unit price as written, null if not stated.",
                        },
                    },
                    "required": ["description"],
                },
            },
            "validity": {
                "type": ["string", "null"],
                "description": "How long the offer holds, as stated ('valid 15 days'). Null if not said.",
            },
            "source_message_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ids of the messages this was read from. Required.",
            },
            "source_quote": {
                "type": "string",
                "description": "The exact wording the quotation was read from, verbatim.",
            },
            "confidence": {
                "type": "number",
                "description": (
                    "0 to 1. Be strict: an ambiguous 'around 500 maybe' is low "
                    "confidence, a clear 'quote attached: Rs 1,25,000' is high."
                ),
            },
        },
        "required": ["found", "source_message_ids", "confidence"],
    },
}

SYSTEM = """You read B2B sales conversations and identify when the business
gave the customer a price quotation.

These conversations are Indian B2B. Expect Hindi and English mixed in one
sentence, amounts written as "2.5L", "1.2 lakh", "Rs 499/pc", and
quantities in pieces, cartons, lots or dozens.

What counts as a quotation:
- The business stating a price for specific goods or services
- A revised price offered during negotiation
- A quote referenced as sent ("quotation bhej diya, 1.25 lakh")

What does NOT count:
- The customer asking for a rate
- The business's general price list shared without reference to this deal
- A payment reminder for an existing invoice
- An order already placed at an agreed price

Rules you must not break:
- Copy amounts exactly as written. Never convert, calculate, or normalise
  them. If the business wrote "2.5L", record "2.5L".
- If the business never stated a number, total_amount is null. Do not
  infer a total by multiplying quantity by unit price.
- Cite only message ids that appear in the conversation given to you.
- source_quote must be text that actually appears in a message, copied
  exactly.
- If unsure whether it is a quotation, set found to false. A missed
  quotation is recoverable; an invented one is not."""


@dataclass(slots=True)
class ExtractedItem:
    description: str
    quantity: str | None = None
    unit_price_paise: int | None = None


@dataclass(slots=True)
class ExtractedQuotation:
    total_paise: int | None
    reference: str | None
    validity_note: str | None
    source_message_ids: list[str]
    source_quote: str | None
    confidence: float
    items: list[ExtractedItem] = field(default_factory=list)


@dataclass(slots=True)
class Extraction:
    quotation: ExtractedQuotation | None
    cost_paise: int
    rejected_reason: str | None = None


# "2.5L" / "1.2 lakh" / "3 cr" - Indian shorthand, which no generic money
# parser handles and which is how these amounts are actually written.
_MULTIPLIERS: tuple[tuple[str, int], ...] = (
    ("crore", 10_000_000),
    ("cr", 10_000_000),
    ("lakh", 100_000),
    ("lac", 100_000),
    ("l", 100_000),
    ("k", 1_000),
)

_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


def parse_amount_paise(text: str | None, *, is_total: bool = True) -> int | None:
    """
    An amount as a human wrote it -> paise, or None when it cannot be read
    confidently.

    None is a real answer here. "499 per piece" has a number but no total,
    and returning 49900 for it as a *total* would put a per-unit price in a
    total field - a wrong number that looks right, which is the worst
    outcome. Read as a *unit price*, that same string is exactly correct,
    which is what `is_total=False` is for.
    """
    if not text:
        return None
    raw = str(text).strip().lower()

    # A per-unit price is not a total. Refuse rather than mislabel - but
    # only when a total is what was asked for.
    if is_total and any(marker in raw for marker in ("per ", "/pc", "/ pc", "each", "per-")):
        return None

    match = _NUMBER.search(raw)
    if match is None:
        return None
    # The number pattern deliberately excludes a sign, so a negative would
    # otherwise be read as positive. A negative quote is not a quote.
    if match.start() > 0 and raw[match.start() - 1] == "-":
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None

    # Look only at what follows the number, so "Rs 5" doesn't match the "l"
    # of an earlier word.
    tail = raw[match.end():].strip()
    for suffix, multiplier in _MULTIPLIERS:
        if tail.startswith(suffix):
            value *= multiplier
            break

    if value <= 0:
        return None
    return int(round(value * 100))


def _render(messages: list[dict]) -> str:
    """Same shape as commitments.py::_render - ids the model must cite."""
    lines = []
    for m in messages:
        who = "Customer" if m["direction"] == "inbound" else "Business"
        when = m["occurred_at"].strftime("%d %b %Y %H:%M")
        lines.append(f"[id: {m['id']}] {when} — {who}: {m['text']}")
    return "\n".join(lines)


async def extract(
    *,
    messages: list[dict],
    business_context: str,
    now: datetime | None = None,
) -> Extraction:
    """
    Read a conversation and return the quotation in it, if there is one.

    `messages` is a list of {id, direction, occurred_at, text}. Anything
    citing an id not in that list is discarded rather than trusted - the
    same rule commitments.py applies, for the same reason.
    """
    now = now or datetime.now(timezone.utc)
    usable = [m for m in messages if (m.get("text") or "").strip()]
    if not usable:
        return Extraction(quotation=None, cost_paise=0)

    valid_ids = {str(m["id"]) for m in usable}

    prompt = (
        f"Business context:\n{business_context}\n\n"
        f"Today is {now.strftime('%d %B %Y')}.\n\n"
        f"Conversation:\n{_render(usable)}\n\n"
        "Did the business quote a price here? If not, set found to false."
    )

    completion = await client.complete(
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        speed="deep",
        tool=EXTRACT_TOOL,
        max_tokens=1024,
    )
    data = completion.tool_input or {}
    cost = completion.cost_paise

    if not data.get("found"):
        return Extraction(quotation=None, cost_paise=cost)

    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < CONFIRM_THRESHOLD:
        return Extraction(
            quotation=None, cost_paise=cost, rejected_reason="below_confidence_threshold"
        )

    cited = [str(i) for i in (data.get("source_message_ids") or []) if str(i) in valid_ids]
    if not cited:
        # A claim with no real evidence behind it is rejected outright,
        # never stored unattributed.
        logger.warning("quotation extraction cited no valid message ids - discarded")
        return Extraction(quotation=None, cost_paise=cost, rejected_reason="no_valid_citation")

    source_quote = data.get("source_quote")
    items: list[ExtractedItem] = []
    for raw_item in data.get("items") or []:
        if not isinstance(raw_item, dict):
            continue
        description = (raw_item.get("description") or "").strip()
        if not description:
            continue
        items.append(
            ExtractedItem(
                description=description,
                quantity=(raw_item.get("quantity") or None),
                unit_price_paise=parse_amount_paise(raw_item.get("unit_price"), is_total=False),
            )
        )

    return Extraction(
        quotation=ExtractedQuotation(
            total_paise=parse_amount_paise(data.get("total_amount")),
            reference=(data.get("reference") or None),
            validity_note=(data.get("validity") or None),
            source_message_ids=cited,
            source_quote=str(source_quote) if source_quote else None,
            confidence=confidence,
            items=items,
        ),
        cost_paise=cost,
    )
