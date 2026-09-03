"""
Suggesting what a staff member could say, live, while they're the one
actually talking to a customer.

Deliberately not agent.py's draft_reply/stream_reply - those decide what
the AI itself will say, then either queue it for approval or speak it
directly. This is a different job: the human on the call is already
talking, and the only thing generated here is a short prompt for what they
could say next, using the exact same business/customer context the reply
agent already reads. Never spoken, never sent anywhere but a screen - a
human's own words remain their own words.
"""

from dataclasses import dataclass

from shared.ai import client
from shared.ai import context as ctx
from shared.utils.logging import get_logger

logger = get_logger(__name__)

SYSTEM = """You are listening to a live phone call between a business's own \
staff member and a customer, and suggesting what the staff member could say \
next - never speaking yourself, never seen by the customer.

You are given the same business details, customer history and outstanding \
commitments the AI reply agent would use to answer this customer directly, \
plus the conversation so far on this call.

Write ONE short suggestion, no more than two sentences, in plain language a \
person could just say out loud. Not a menu of options, not a script - the \
single most useful thing to say next given what was just said.

Rules:
- Never invent a fact. Prices, dates, availability and policies come from \
the business details you were given, or leave them out of the suggestion \
entirely - a wrong price suggested to a staff member is worse than no \
suggestion.
- If the last thing said needs no particular guidance - small talk, a \
pleasantry, something the staff member obviously already knows how to \
answer - respond with exactly the word NONE and nothing else, rather than \
manufacturing a suggestion nobody needs.
- Be concrete. "Mention her outstanding balance" is worse than "She has \
₹4,500 outstanding from March - ask if she'd like to settle it today.\""""


@dataclass(slots=True)
class Suggestion:
    text: str | None  # None means nothing worth suggesting right now
    cost_paise: int


async def suggest(agent_context: ctx.AgentContext) -> Suggestion:
    """One short suggestion for a staff member mid-call, or nothing."""
    prompt = (
        f"Today is {ctx.now_line()}.\n\n"
        f"{agent_context.render()}\n\n"
        "What could the staff member say next?"
    )

    completion = await client.complete(
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        speed="fast",
        max_tokens=150,
    )

    text = completion.text.strip()
    if not text or text.upper() == "NONE":
        return Suggestion(text=None, cost_paise=completion.cost_paise)
    return Suggestion(text=text, cost_paise=completion.cost_paise)
