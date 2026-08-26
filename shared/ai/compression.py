"""
Turning a customer's history into something an agent can afford to read.

The cold path's real job, and the reason the hot path can be fast. Two hundred
messages will not fit in a prompt a caller is waiting through - and even where
the budget allows it, answer quality falls as context grows. So the overnight
worker reads everything once and writes down the five lines that matter.

What those five lines contain is the whole design decision. Not a summary of
what was said, which nobody needs, but what a person picking up this
conversation would want to know before speaking:

    who they are and how long they have been a customer
    what is unresolved between you
    how they behave - do they pay on time, do they reply, what do they ask for
    anything to be careful about

Written in the second person, because it is read aloud in effect: the agent
uses it mid-conversation, and "she usually pays late" is easier to act on than
a paragraph of narration.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from shared.ai import client
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# How much history to compress in one pass. Beyond this the oldest messages
# add little - a customer's behaviour last year is not what the agent needs
# mid-sentence.
MAX_MESSAGES = 200

# Below this there is nothing worth compressing; the agent can read the raw
# turns and a summary would only add noise.
MIN_MESSAGES = 4

SUMMARY_TOOL = {
    "name": "record_customer",
    "description": "Write down what a person answering this customer should know.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "Three to five short sentences, second person, addressed to "
                    "the business owner. What would you tell a colleague who is "
                    "about to speak to this customer? Facts and patterns only - "
                    "no narration of the conversation."
                ),
            },
            "preferences": {
                "type": "object",
                "description": (
                    "Concrete things worth remembering: language they write in, "
                    "how they like to be contacted, what they usually buy, "
                    "anything to avoid. Omit anything you are unsure of."
                ),
                "properties": {
                    "language": {"type": "string"},
                    "buys": {"type": "string"},
                    "avoid": {"type": "string"},
                    "notes": {"type": "string"},
                },
            },
            "health_score": {
                "type": "integer",
                "description": (
                    "0 to 100. How healthy is this relationship? Above 70 means "
                    "they pay, reply and are easy to deal with. Below 30 means "
                    "unpaid promises, ignored messages or friction. 50 if there "
                    "is not enough to judge."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "One sentence on why that score.",
            },
        },
        "required": ["summary", "health_score", "reasoning"],
    },
}


SYSTEM = """You compress a customer's history into what a business owner \
needs to know before speaking to them.

You are given the whole conversation and what is currently outstanding. Write \
the note a good office manager would leave on a file.

What to include:
- Who they are, and roughly how long they have been dealing with this business
- What is unresolved right now
- Patterns worth acting on: do they pay on time, do they reply quickly, what \
do they usually ask for, do they negotiate
- Anything to be careful about - a complaint, a dispute, a sensitivity

What to leave out:
- A retelling of the conversation. The agent can read that if it needs to.
- Anything you inferred without evidence. If they have paid once, you do not \
know that they "always pay promptly".
- Praise or judgement. "Good customer" tells nobody anything.

Write in the second person, addressed to the owner: "She usually pays within \
a week of asking." Short sentences. Three to five of them.

Be honest about thin evidence. With two messages to go on, say what you saw \
and score the relationship 50 rather than inventing a personality."""


@dataclass(slots=True)
class Profile:
    summary: str
    preferences: dict
    health_score: int
    reasoning: str
    cost_paise: int
    source_message_ids: list[uuid.UUID]


def _render(messages: list[dict], commitments: list[dict], now: datetime) -> str:
    lines: list[str] = []

    if commitments:
        lines.append("Currently outstanding:")
        for c in commitments:
            side = "You owe them" if c["direction"] == "we_owe" else "They owe you"
            amount = f" ({c['amount']})" if c.get("amount") else ""
            due = f", due {c['due']}" if c.get("due") else ""
            state = c.get("status", "open")
            lines.append(f"- {side}: {c['description']}{amount}{due} [{state}]")
        lines.append("")

    lines.append("The conversation:")
    for m in messages:
        speaker = "Customer" if m["direction"] == "inbound" else "Business"
        when = m["occurred_at"].strftime("%d %b %Y")
        channel = m.get("channel", "")
        text = (m.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{when}] [{channel}] {speaker}: {text[:400]}")

    return "\n".join(lines)


async def compress(
    *,
    messages: list[dict],
    commitments: list[dict],
    customer_name: str | None,
    first_seen: datetime | None,
    business_context: str,
    now: datetime | None = None,
) -> Profile | None:
    """
    Read a customer's whole history and write down what matters.

    Returns None when there is too little to compress - a summary built from
    two messages is worse than no summary, because the agent would treat an
    invented pattern as fact.
    """
    now = now or datetime.now(timezone.utc)
    usable = [m for m in messages if (m.get("text") or "").strip()]

    if len(usable) < MIN_MESSAGES:
        return None

    trimmed = usable[-MAX_MESSAGES:]
    since = first_seen.strftime("%B %Y") if first_seen else "unknown"

    prompt = (
        f"Business: {business_context}\n"
        f"Customer: {customer_name or 'name unknown'}\n"
        f"First contact: {since}\n"
        f"Today: {now.strftime('%d %B %Y')}\n"
        f"Messages exchanged: {len(usable)}\n\n"
        f"{_render(trimmed, commitments, now)}\n\n"
        "Write the note."
    )

    completion = await client.complete(
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        speed="deep",
        tool=SUMMARY_TOOL,
        max_tokens=1024,
    )

    result = completion.tool_input or {}
    summary = (result.get("summary") or "").strip()
    if not summary:
        logger.warning("compression returned no summary")
        return None

    try:
        score = int(result.get("health_score", 50))
    except (TypeError, ValueError):
        score = 50
    score = min(100, max(0, score))

    preferences = result.get("preferences")
    if not isinstance(preferences, dict):
        preferences = {}
    # Drop empty values rather than storing {"language": ""}, which reads as a
    # fact we established when it is the opposite.
    preferences = {k: v for k, v in preferences.items() if isinstance(v, str) and v.strip()}

    return Profile(
        summary=summary,
        preferences=preferences,
        health_score=score,
        reasoning=(result.get("reasoning") or "").strip(),
        cost_paise=completion.cost_paise,
        source_message_ids=[m["id"] for m in trimmed],
    )
