"""
Finding promises in conversations.

This is the core of the product. Every business conversation contains
obligations in both directions - "I'll pay by Friday", "we'll deliver
Tuesday", "I'll call you back" - and every other platform treats them as text.
Reading them as obligations is what turns a message log into a picture of what
a business is owed and what it owes, weeks before any of it reaches
accounting.

The extraction is designed around one asymmetry: an invented commitment is far
worse than a missed one. Telling an owner "they promised you Rs 45,000" when
nobody said that destroys trust in every other number on the screen, and the
owner has no way to know which figures to doubt. A missed promise is merely a
gap.

So three rules hold throughout:

  Every commitment must cite the message it came from, and those ids are
  checked against the database. A citation we cannot find means the model
  invented it, and the whole extraction is rejected rather than half-trusted.

  Amounts are only recorded when a number was actually stated. No inferring
  a price from context.

  Anything uncertain is stored as unconfirmed, shown to the owner, and never
  acted on until they say yes.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from shared.ai import client
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Below this, a commitment is recorded but marked unconfirmed and waits for a
# human. Chosen deliberately high: the cost of a wrong commitment is an owner
# chasing a customer for money they were never promised.
CONFIRM_THRESHOLD = 0.75

EXTRACT_TOOL = {
    "name": "record_commitments",
    "description": (
        "Record every promise found in the conversation. Return an empty list "
        "if there are none - most conversations contain none, and that is the "
        "correct answer."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "commitments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "direction": {
                            "type": "string",
                            "enum": ["we_owe", "they_owe"],
                            "description": (
                                "we_owe: the business promised the customer "
                                "something. they_owe: the customer promised "
                                "the business something."
                            ),
                        },
                        "kind": {
                            "type": "string",
                            "enum": [
                                "payment",
                                "delivery",
                                "callback",
                                "document",
                                "meeting",
                                "other",
                            ],
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "What was promised, in the words the "
                                "conversation used. One short sentence."
                            ),
                        },
                        "amount_paise": {
                            "type": ["integer", "null"],
                            "description": (
                                "Amount in paise (Rs 1 = 100 paise), ONLY if a "
                                "figure was actually stated. Never estimate or "
                                "infer an amount. null otherwise."
                            ),
                        },
                        "due_at": {
                            "type": ["string", "null"],
                            "description": (
                                "ISO 8601 date/time the promise falls due, if "
                                "determinable. null if no timeframe was given."
                            ),
                        },
                        "due_at_explicit": {
                            "type": "boolean",
                            "description": (
                                "true if an actual date or day was stated. "
                                "false if inferred from vague wording like "
                                "'next week' or 'soon'."
                            ),
                        },
                        "source_message_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "The message ids this was read from. Use ONLY "
                                "ids that appear in the conversation given to "
                                "you. Required."
                            ),
                        },
                        "source_quote": {
                            "type": "string",
                            "description": (
                                "The exact words that carried the promise, "
                                "copied verbatim from the message."
                            ),
                        },
                        "confidence": {
                            "type": "number",
                            "description": (
                                "0 to 1. Use below 0.75 when the promise is "
                                "implied rather than stated plainly."
                            ),
                        },
                    },
                    "required": [
                        "direction",
                        "kind",
                        "description",
                        "source_message_ids",
                        "source_quote",
                        "confidence",
                    ],
                },
            }
        },
        "required": ["commitments"],
    },
}


SYSTEM = """You read business conversations and find promises.

A promise is a specific obligation someone took on: to pay, to deliver, to
send something, to call back, to attend. Both directions count - what the
business promised the customer, and what the customer promised the business.

RECORD these. They are promises:
- "I'll email the invoice by Monday"            -> we_owe, document
- "I'll transfer Rs 4,500 on Friday"            -> they_owe, payment
- "We'll deliver on Tuesday"                    -> we_owe, delivery
- "I'll call you back this evening"             -> we_owe, callback
- "I'll come in at 4pm tomorrow"                -> they_owe, meeting
- "Payment will be done by month end"           -> they_owe, payment

DO NOT record these. They are not promises:
- Questions: "how much is a cleaning?"
- Prices quoted or availability stated: "cleaning is Rs 1,200"
- Pleasantries: greetings, thanks, apologies
- Hypotheticals: "if we go ahead, we'd pay in 30 days"
- Vague intentions with no obligation: "we'll be in touch sometime"

Judgement, in order of importance:

1. Cite your evidence. Every commitment must list the message ids it came
from, using only ids present in the conversation you were given. Never invent
an id.

2. Quote the actual words. source_quote is copied verbatim from the message,
never paraphrased.

3. Never invent an amount. Record amount_paise only when a figure was actually
stated. "I'll pay soon" has no amount - use null. A wrong amount is worse than
no amount.

4. Record what was clearly promised. If someone plainly committed to something,
record it - that is the job. Do not talk yourself out of an obvious promise.

5. Be honest about certainty. A promise stated plainly gets high confidence.
One that is implied, conditional or vague gets below 0.75 so a human checks it
before anyone acts on it.

6. Attachments are evidence, not speech. When someone sends an invoice,
receipt or screenshot, its contents are shown to you as an attachment. Figures
and dates printed on it are real and may be used - an invoice saying "TOTAL DUE
Rs 5,000, payable by 28 August" together with the customer writing "I'll pay
it" is a payment commitment of Rs 5,000 due 28 August. Cite the attachment's
id as your source.

7. When a conversation genuinely contains no promises, return an empty list.
Never manufacture one to seem useful."""


@dataclass(slots=True)
class ExtractedCommitment:
    direction: str
    kind: str
    description: str
    amount_paise: int | None
    due_at: datetime | None
    due_at_explicit: bool
    source_message_ids: list[uuid.UUID]
    source_quote: str
    confidence: float


@dataclass(slots=True)
class Extraction:
    commitments: list[ExtractedCommitment]
    cost_paise: int
    rejected: int  # citations that pointed at messages we do not have


def _parse_due(value: str | None, now: datetime) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    # A due date decades out is a parsing artefact, not a promise.
    if abs(parsed - now) > timedelta(days=3650):
        return None
    return parsed


def _as_list(value) -> list:
    """
    Coerce the tool result into a list of items.

    Observed in practice: the model sometimes hands back the array as a raw
    JSON *string* rather than a parsed list. Iterating that yields one
    character at a time, which produced hundreds of nonsense "malformed item"
    rejections before this guard existed - and, worse, would have silently
    reported a conversation as having no commitments.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            logger.warning("commitments came back as unparseable text, discarding")
            return []
        return parsed if isinstance(parsed, list) else []
    if value is None:
        return []
    logger.warning("commitments came back as %s, discarding", type(value).__name__)
    return []


def _as_confidence(value) -> float:
    """Coerce whatever came back into a usable 0-1 score, erring low."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        # No usable score means treat it as uncertain, so a human checks it.
        return 0.5
    return min(1.0, max(0.0, score))


def _render(messages: list[dict]) -> str:
    """
    Lay out the conversation with ids the model must cite.

    Attachments are marked as attachments. A photographed invoice reaches us
    as a description we wrote, and rendering that as speech tells the model
    the customer said it - which breaks the link between "I'll pay it" and the
    amount printed on the invoice they sent.
    """
    lines = []
    for m in messages:
        who = "Customer" if m["direction"] == "inbound" else "Business"
        when = m["occurred_at"].strftime("%d %b %Y %H:%M")
        if m.get("is_attachment"):
            lines.append(
                f"[id: {m['id']}] {when} — {who} sent an attachment. "
                f"Its contents:\n{m['text']}"
            )
        else:
            lines.append(f"[id: {m['id']}] {when} — {who}: {m['text']}")
    return "\n".join(lines)


async def extract(
    *,
    messages: list[dict],
    business_context: str,
    now: datetime | None = None,
) -> Extraction:
    """
    Read a conversation and return the promises in it.

    `messages` is a list of {id, direction, occurred_at, text}. Any commitment
    citing an id not in that list is discarded - see the module docstring.
    """
    now = now or datetime.now(timezone.utc)
    usable = [m for m in messages if (m.get("text") or "").strip()]
    if not usable:
        return Extraction(commitments=[], cost_paise=0, rejected=0)

    valid_ids = {str(m["id"]) for m in usable}

    prompt = (
        f"Business context:\n{business_context}\n\n"
        f"Today is {now.strftime('%d %B %Y')}.\n\n"
        f"Conversation:\n{_render(usable)}\n\n"
        "Record every promise in this conversation. If there are none, return "
        "an empty list."
    )

    completion = await client.complete(
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        speed="deep",
        tool=EXTRACT_TOOL,
        max_tokens=2048,
    )

    raw = _as_list((completion.tool_input or {}).get("commitments"))
    found: list[ExtractedCommitment] = []
    rejected = 0

    for item in raw:
        # Structured output constrains the shape but does not guarantee it -
        # observed in practice returning a bare string where the schema says
        # object. A malformed item is dropped, never guessed at.
        if not isinstance(item, dict):
            rejected += 1
            logger.warning("discarded malformed commitment item: %r", str(item)[:120])
            continue

        raw_ids = item.get("source_message_ids")
        if not isinstance(raw_ids, list):
            raw_ids = []
        cited = [str(i) for i in raw_ids if isinstance(i, (str, uuid.UUID))]
        kept = []
        for candidate in cited:
            if candidate not in valid_ids:
                continue
            try:
                uuid.UUID(candidate)
            except ValueError:
                # An id shaped like ours but not a UUID. Would raise below.
                continue
            kept.append(candidate)

        if not kept:
            # Every citation pointed at something we did not send. The model
            # invented its evidence, so the claim goes in the bin rather than
            # into a business owner's receivables.
            rejected += 1
            logger.warning(
                "rejected commitment with unknown citations %s: %r",
                cited,
                (item.get("description") or "")[:80],
            )
            continue

        if len(kept) != len(cited):
            logger.info("commitment cited %s unknown ids, keeping the valid ones",
                        len(cited) - len(kept))

        amount = item.get("amount_paise")
        if amount is not None and (not isinstance(amount, int) or amount < 0):
            amount = None

        found.append(
            ExtractedCommitment(
                direction=(
                    item.get("direction")
                    if item.get("direction") in ("we_owe", "they_owe")
                    else "they_owe"
                ),
                kind=(
                    item.get("kind")
                    if item.get("kind")
                    in ("payment", "delivery", "callback", "document", "meeting", "other")
                    else "other"
                ),
                description=(item.get("description") or "").strip(),
                amount_paise=amount,
                due_at=_parse_due(item.get("due_at"), now),
                due_at_explicit=bool(item.get("due_at_explicit")),
                source_message_ids=[uuid.UUID(c) for c in kept],
                source_quote=(item.get("source_quote") or "").strip(),
                confidence=_as_confidence(item.get("confidence")),
            )
        )

    return Extraction(
        commitments=found, cost_paise=completion.cost_paise, rejected=rejected
    )
