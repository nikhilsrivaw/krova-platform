"""
Finding product feedback in conversations - bugs, feature requests,
complaints, churn risk, praise.

Same discipline as commitments.py, aimed at a different table: Insight
already existed in the schema ("something worth telling the owner, with the
evidence attached" - overdue_payment | demand_signal | competitor_mention |
churn_risk was the docstring's own example list) but nothing had ever
written a row to it. This is what makes it real.

The same citation rule holds: a signal citing a message id we cannot find is
discarded, not half-trusted. Telling a founder "a user is about to churn"
when nobody said anything like that is worse than missing a real one.
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
                            "enum": ["bug", "feature_request", "complaint", "churn_risk", "praise"],
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
                                "loss. warning: a real bug, or a pointed complaint. "
                                "info: a feature request or praise."
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

SYSTEM = """You read product/user conversations for an early-stage company \
and find signals worth a founder's attention.

RECORD these:
- "This crashes every time I try to export"        -> bug
- "Can you add dark mode?"                          -> feature_request
- "This is the third time support hasn't replied"   -> complaint
- "We're evaluating [competitor] instead"           -> churn_risk
- "Cancel my account"                               -> churn_risk
- "This update is exactly what we needed"           -> praise

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


async def extract(
    *,
    messages: list[dict],
    business_context: str,
    now: datetime | None = None,
) -> Extraction:
    """
    Read a conversation and return the product feedback signals in it.

    `messages` is a list of {id, direction, occurred_at, text}. Any signal
    citing an id not in that list is discarded - see the module docstring.
    """
    now = now or datetime.now(timezone.utc)
    usable = [m for m in messages if (m.get("text") or "").strip()]
    if not usable:
        return Extraction(signals=[], cost_paise=0, rejected=0)

    valid_ids = {str(m["id"]) for m in usable}

    prompt = (
        f"Business context:\n{business_context}\n\n"
        f"Today is {now.strftime('%d %B %Y')}.\n\n"
        f"Conversation:\n{_render(usable)}\n\n"
        "Record every product feedback signal in this conversation. If there "
        "are none, return an empty list."
    )

    completion = await client.complete(
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        speed="deep",
        tool=EXTRACT_TOOL,
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
        if kind not in ("bug", "feature_request", "complaint", "churn_risk", "praise"):
            kind = "complaint"
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
