"""
Finding product feedback in conversations - bugs, feature requests,
complaints, churn risk, praise - plus, for a startup selling to WhatsApp-
native SMB buyers where a pricing/demo conversation happens in the same
channel as support, demo_request and pricing_question.

Same discipline as commitments.py, aimed at a different table: Insight
already existed in the schema ("something worth telling the owner, with the
evidence attached" - overdue_payment | demand_signal | competitor_mention |
churn_risk was the docstring's own example list) but nothing had ever
written a row to it. This is what makes it real.

The same citation rule holds: a signal citing a message id we cannot find is
discarded, not half-trusted. Telling a founder "a user is about to churn"
when nobody said anything like that is worse than missing a real one.

Two modes, since 2026-09-25:

  product (include_product=True) - the original eight kinds, for a business
    with the product_feedback capability. Unchanged.

  conversation (include_product=False) - every other business. Only the
    four kinds that mean something for any business talking to its
    customers: complaint, churn_risk, praise, competitor_mention. Before
    this mode existed, a PG owner whose tenant wrote "next month se chhod
    raha hu", or a laser clinic whose client wrote "4th session ke baad bhi
    result nahi", got no signal at all - the whole extractor sat behind a
    startup-only capability, with a prompt written for a founder reading
    bug reports. Research on recurring-revenue businesses is consistent that
    the earliest churn signal is conversational (docs/new/type-2-generic.md),
    and conversations are the one thing this product reads.

    pricing_question and demo_request are left out of this mode on purpose.
    For a startup they are rare and worth a founder's eye; for a coaching
    institute or a gym, "fees kitni hai" and "trial mil sakta hai?" are
    every single enquiry, and flagging each one would bury the four signals
    that actually matter. Bookings have their own triggers already.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from shared.ai import client
from shared.utils.logging import get_logger

logger = get_logger(__name__)

EXTRACT_TOOL = {
    "name": "record_signals",
    "description": (
        "Record every product feedback signal in the conversation - bugs, "
        "feature requests, complaints, churn risk, praise. Return an empty "
        "list if there are none - most messages contain none, and that is "
        "the correct answer."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "signals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "bug", "feature_request", "complaint", "churn_risk", "praise",
                                "demo_request", "pricing_question", "competitor_mention",
                            ],
                        },
                        "title": {
                            "type": "string",
                            "description": "One short line summarising it, for a list view.",
                        },
                        "body": {
                            "type": "string",
                            "description": "More detail, in the words the conversation used.",
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["info", "warning", "critical"],
                            "description": (
                                "critical: churn risk, or a bug touching billing/security/data "
                                "loss. warning: a real bug, a pointed complaint, or a competitor "
                                "mention that reads as active evaluation (not just curiosity). "
                                "info: a feature request, praise, or a competitor mentioned only "
                                "in passing."
                            ),
                        },
                        "source_message_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "The message ids this was read from. Use ONLY ids "
                                "that appear in the conversation given to you. Required."
                            ),
                        },
                        "source_quote": {
                            "type": "string",
                            "description": "The exact words, copied verbatim from the message.",
                        },
                    },
                    "required": ["kind", "title", "severity", "source_message_ids", "source_quote"],
                },
            }
        },
        "required": ["signals"],
    },
}

PRODUCT_KINDS = (
    "bug", "feature_request", "complaint", "churn_risk", "praise",
    "demo_request", "pricing_question", "competitor_mention",
)
CONVERSATION_KINDS = ("complaint", "churn_risk", "praise", "competitor_mention")

SYSTEM = """You read product/user conversations for an early-stage company \
and find signals worth a founder's attention.

RECORD these:
- "This crashes every time I try to export"        -> bug
- "Can you add dark mode?"                          -> feature_request
- "This is the third time support hasn't replied"   -> complaint
- "Cancel my account"                               -> churn_risk
- "This update is exactly what we needed"           -> praise
- "Can we get a demo?"                              -> demo_request
- "What's your pricing for 50 seats?"                -> pricing_question
- "We're evaluating [competitor] instead"           -> competitor_mention (also churn_risk if
  it reads as a real intent to leave, not just comparison-shopping - both signals can apply
  to the same message)
- "Do you have anything like [competitor]'s X?"     -> competitor_mention only, not churn_risk -
  a feature comparison, not a threat to leave

DO NOT RECORD these:
- Plain how-to questions with no frustration or problem behind them:
  "how do I reset my password?"
- Pleasantries: greetings, thanks, sign-offs
- Small talk unrelated to the product

Judgement, in order of importance:

1. Cite your evidence. Every signal must list the message ids it came from,
using only ids present in the conversation you were given. Never invent one.

2. Quote the actual words. source_quote is copied verbatim, never paraphrased.

3. Severity is about urgency to a founder, not politeness. A calmly-worded
"I'm switching to your competitor" is critical. An angry-sounding but minor
UI complaint is not.

4. One conversation can contain several signals - a bug report and a feature
request in the same message thread are two separate signals, not one.

5. When a conversation genuinely contains no signal, return an empty list.
Never manufacture one to seem useful."""


CONVERSATION_SYSTEM = """You read conversations between a business and its \
customers and find the few moments the owner needs to know about. The \
business could be anything - a gym, a coaching institute, a library, a PG, a \
salon or clinic package, an accountant on retainer, an AC service contract, \
a shop. Customers write in English, Hindi, or a mix of the two.

RECORD these four, and only these:

- complaint: dissatisfaction with the service, staff, quality, results, or
  how they were treated.
    "Teacher bilkul nahi padhate"                     -> complaint
    "4th session ke baad bhi result nahi dikh raha"   -> complaint
    "Technician do baar aaya, AC abhi bhi kharab"     -> complaint

- churn_risk: they are leaving, stopping, not renewing, pausing without a
  return date, or drifting away. Often calm and polite - that makes it no
  less serious.
    "Next month se PG chhod raha hu"                  -> churn_risk
    "Is saal AMC renew nahi karwayenge"               -> churn_risk
    "Kuch din nahi aa paunga, dekhta hu agle mahine"  -> churn_risk (drifting)
    "Beta ab class nahi jaana chahta"                 -> churn_risk

- competitor_mention: another provider named or clearly meant, especially as
  an alternative they are considering.
    "Kisi aur agency se baat kar rahe hain"           -> competitor_mention
      (and churn_risk too - both can apply to one message)
    "Paas wale gym mein 800 mein ho raha hai"         -> competitor_mention

- praise: genuine, specific appreciation worth the owner seeing.
    "Sir aapki wajah se selection hua, thank you"     -> praise
    Not a plain "thanks" or "ok thank you".

DO NOT RECORD:
- Ordinary enquiries: fees, timings, availability, "trial mil sakta hai?"
- Promises to pay or attend - those are handled elsewhere
- Pleasantries, greetings, thanks, sign-offs
- A single missed day with a clear return ("kal nahi aa paunga, parso aaunga")

Judgement, in order of importance:

1. Cite your evidence. Every signal must list the message ids it came from,
using only ids present in the conversation you were given. Never invent one.

2. Quote the actual words. source_quote is copied verbatim, never
paraphrased, never translated.

3. Only what the CUSTOMER said. The business's own messages are context, not
signals.

4. Severity is about what the owner stands to lose, not tone:
   critical - they are leaving or have decided not to renew; a refund, legal
     or fraud threat.
   warning - a real complaint; drifting away; seriously weighing a competitor.
   info - praise; a competitor mentioned only in passing.

5. When there is genuinely nothing, return an empty list. Most messages
contain nothing, and that is the correct answer. Never manufacture one."""


@dataclass(slots=True)
class ExtractedSignal:
    kind: str
    title: str
    body: str | None
    severity: str
    source_message_ids: list[uuid.UUID]
    source_quote: str


@dataclass(slots=True)
class Extraction:
    signals: list[ExtractedSignal]
    cost_paise: int
    rejected: int


def _as_list(value) -> list:
    """See commitments._as_list - same tool-output-shape defence, same reason."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            logger.warning("signals came back as unparseable text, discarding")
            return []
        return parsed if isinstance(parsed, list) else []
    if value is None:
        return []
    logger.warning("signals came back as %s, discarding", type(value).__name__)
    return []


def _render(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        who = "User" if m["direction"] == "inbound" else "Business"
        when = m["occurred_at"].strftime("%d %b %Y %H:%M")
        lines.append(f"[id: {m['id']}] {when} — {who}: {m['text']}")
    return "\n".join(lines)


def _tool(kinds: tuple[str, ...]) -> dict:
    """EXTRACT_TOOL with its `kind` enum narrowed to this mode's kinds, so
    the model cannot even return a kind this business doesn't get."""
    tool = json.loads(json.dumps(EXTRACT_TOOL))
    item = tool["input_schema"]["properties"]["signals"]["items"]
    item["properties"]["kind"]["enum"] = list(kinds)
    if kinds is CONVERSATION_KINDS:
        tool["description"] = (
            "Record every complaint, churn risk, competitor mention or piece of "
            "praise from the customer. Return an empty list if there are none - "
            "most messages contain none, and that is the correct answer."
        )
        item["properties"]["severity"]["description"] = (
            "critical: leaving, not renewing, or a refund/legal/fraud threat. "
            "warning: a real complaint, drifting away, or seriously weighing a "
            "competitor. info: praise, or a competitor mentioned in passing."
        )
    return tool


async def extract(
    *,
    messages: list[dict],
    business_context: str,
    now: datetime | None = None,
    include_product: bool = True,
) -> Extraction:
    """
    Read a conversation and return the signals in it.

    `messages` is a list of {id, direction, occurred_at, text}. Any signal
    citing an id not in that list is discarded - see the module docstring.

    `include_product` picks the mode (module docstring): True is the
    original product-feedback extractor, False the business-neutral one.
    The default stays True so an existing caller's behaviour cannot change
    by accident.
    """
    kinds = PRODUCT_KINDS if include_product else CONVERSATION_KINDS
    now = now or datetime.now(timezone.utc)
    usable = [m for m in messages if (m.get("text") or "").strip()]
    if not usable:
        return Extraction(signals=[], cost_paise=0, rejected=0)

    valid_ids = {str(m["id"]) for m in usable}

    prompt = (
        f"Business context:\n{business_context}\n\n"
        f"Today is {now.strftime('%d %B %Y')}.\n\n"
        f"Conversation:\n{_render(usable)}\n\n"
        + (
            "Record every product feedback signal in this conversation. If there "
            "are none, return an empty list."
            if include_product
            else "Record every complaint, churn risk, competitor mention or piece of "
            "praise from the customer. If there are none, return an empty list."
        )
    )

    completion = await client.complete(
        system=SYSTEM if include_product else CONVERSATION_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        speed="deep",
        tool=_tool(kinds),
        max_tokens=2048,
    )

    raw = _as_list((completion.tool_input or {}).get("signals"))
    found: list[ExtractedSignal] = []
    rejected = 0

    for item in raw:
        if not isinstance(item, dict):
            rejected += 1
            logger.warning("discarded malformed signal item: %r", str(item)[:120])
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
                continue
            kept.append(candidate)

        if not kept:
            rejected += 1
            logger.warning(
                "rejected signal with unknown citations %s: %r",
                cited, (item.get("title") or "")[:80],
            )
            continue

        kind = item.get("kind")
        if kind not in kinds:
            # Outside this mode's set (the tool schema should already
            # prevent it). Dropped rather than coerced to "complaint", as the
            # product mode always did: in conversation mode a stray "bug" is
            # far more likely to be nothing than to be a complaint.
            if include_product:
                kind = "complaint"
            else:
                rejected += 1
                continue
        severity = item.get("severity")
        if severity not in ("info", "warning", "critical"):
            severity = "info"

        title = (item.get("title") or "").strip()
        if not title:
            rejected += 1
            continue

        found.append(
            ExtractedSignal(
                kind=kind,
                title=title[:255],
                body=(item.get("body") or "").strip() or None,
                severity=severity,
                source_message_ids=[uuid.UUID(i) for i in kept],
                source_quote=(item.get("source_quote") or "").strip(),
            )
        )

    return Extraction(signals=found, cost_paise=completion.cost_paise, rejected=rejected)
