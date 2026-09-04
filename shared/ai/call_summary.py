"""
A structured read on how one finished call went - outcome, sentiment,
topic, a one-line summary - written onto the Call row itself.

Deliberately not signals.py: that module looks for noteworthy product-
feedback signals (a bug, a churn risk) and only runs for businesses whose
vertical declares product_feedback (today, only "startup"). This is a
different, universal question - "how did this call go" - that applies to
every call on every vertical, so it needs no capability gate and no
per-vertical taxonomy. `topic` is free text rather than an enum for the
same reason: a clinic's topics and a real-estate agency's topics look
nothing alike, and hardcoding a shared list would mean picking one
vertical's shape and forcing the rest into it.

Only reads the transcript, not shared/ai/context.py's full AgentContext -
classifying outcome/sentiment/topic needs no business policy detail or
customer history, and building that context requires a customer_id this
call may not cleanly resolve to by the time this runs.
"""

from dataclasses import dataclass

from shared.ai import client
from shared.utils.logging import get_logger

logger = get_logger(__name__)

SUMMARIZE_TOOL = {
    "name": "record_call_summary",
    "description": "Record a structured read on how this phone call went.",
    "input_schema": {
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "enum": ["resolved", "escalated", "booked", "no_action"],
                "description": (
                    "resolved: the caller's question was answered. escalated: "
                    "handed off or the agent could not help. booked: an "
                    "appointment/booking was made on this call. no_action: "
                    "nothing needed doing - a wrong number, small talk, a "
                    "call that ended before anything happened."
                ),
            },
            "sentiment": {
                "type": "string",
                "enum": ["positive", "neutral", "negative"],
                "description": "The caller's overall tone across the call.",
            },
            "topic": {
                "type": "string",
                "description": (
                    "One short phrase for what the call was about, e.g. "
                    "'Appointment scheduling' or 'Pricing question'."
                ),
            },
            "summary": {
                "type": "string",
                "description": "One plain-English sentence summarising what happened on the call.",
            },
        },
        "required": ["outcome", "sentiment", "topic", "summary"],
    },
}

SYSTEM = """You read the transcript of a finished phone call between a \
business's AI phone agent and a caller, and record a short, structured \
read on how it went - for the business owner to scan later, not for the \
caller to see.

Judgement:
- outcome is about what actually happened, not what was attempted. If the \
agent tried to help but the caller hung up frustrated, that is escalated \
or negative sentiment, not resolved.
- topic should be specific enough to be useful across many calls - "Asked \
about Saturday appointment availability" is better than "Question", but \
still one short phrase, not a paragraph.
- summary is one sentence a busy owner can read in two seconds and know \
whether this call needs their attention."""


@dataclass(slots=True)
class CallSummary:
    outcome: str
    sentiment: str
    topic: str
    summary: str
    cost_paise: int


def _render(transcript: list[dict]) -> str:
    lines = []
    for turn in transcript:
        who = "Caller" if turn.get("role") == "caller" else "Agent"
        lines.append(f"{who}: {turn.get('text', '')}")
    return "\n".join(lines)


async def summarize(
    transcript: list[dict], *, business_name: str | None = None
) -> CallSummary | None:
    """
    One structured read on a finished call, or None if there is nothing
    worth summarising (an empty transcript).
    """
    if not transcript:
        return None

    business_line = f"Business: {business_name}\n\n" if business_name else ""
    prompt = (
        f"{business_line}Call transcript:\n{_render(transcript)}\n\n"
        "Record how this call went."
    )

    completion = await client.complete(
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        speed="fast",
        tool=SUMMARIZE_TOOL,
        max_tokens=300,
    )

    tool_input = completion.tool_input or {}
    outcome = tool_input.get("outcome")
    if outcome not in ("resolved", "escalated", "booked", "no_action"):
        outcome = "no_action"
    sentiment = tool_input.get("sentiment")
    if sentiment not in ("positive", "neutral", "negative"):
        sentiment = "neutral"
    topic = (tool_input.get("topic") or "").strip()[:255] or "General inquiry"
    summary = (tool_input.get("summary") or "").strip()
    if not summary:
        logger.warning("call summary came back with no summary text, keeping structured fields anyway")
        summary = "No summary available."

    return CallSummary(
        outcome=outcome,
        sentiment=sentiment,
        topic=topic,
        summary=summary,
        cost_paise=completion.cost_paise,
    )
