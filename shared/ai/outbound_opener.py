"""
Drafting the first thing to say on an outbound call.

Every inbound call has a fixed greeting - "Hello, thank you for calling
X" - because the caller started the conversation and the agent is
responding. An outbound call has no prior turn to respond to: KROVA
placed it, for a reason, and the person who just picked up needs to hear
why in the first sentence or two, grounded in the same real business/
customer data every other prompt in this codebase already reads - never
a fixed script, and never a fact this call's own objective and context
don't actually support.
"""

from dataclasses import dataclass

from shared.ai import client
from shared.ai import context as ctx
from shared.utils.logging import get_logger

logger = get_logger(__name__)

SYSTEM = """You are opening a phone call the business placed on its own \
initiative - the person who just answered did not call in, so they have \
no idea yet why the phone rang.

You are given the business's details, what's known about this customer, \
what's outstanding between them, and the reason this specific call was \
placed. Write the first thing the agent should say - who is calling, in \
one breath, then the reason for the call.

Rules:
- Identify the business by name in the first sentence. A call that opens \
without saying who is calling sounds like a scam call, and people hang \
up on those.
- Never invent a fact. State only what the business details, the \
customer's outstanding commitments, and the stated reason for this call \
actually support - if the reason for calling references an amount or \
date, use only figures you were actually given.
- Keep it to one or two short sentences, spoken naturally - this is what \
gets said out loud the instant someone says "hello", not written prose.
- Do not ask a question yet. State why you're calling; let the person \
respond first, the same way any real phone call opens."""


@dataclass(slots=True)
class Opener:
    text: str
    cost_paise: int


async def draft(agent_context: ctx.AgentContext, *, reason: str) -> Opener:
    """
    Draft the opening line for an outbound call.

    `reason` is the call campaign's own objective, e.g. "Remind them about
    the outstanding balance and offer to help them pay" - a brief, not a
    script; everything after this opener is the same live agent loop
    (shared/ai/agent.py's stream_reply) any inbound call already uses.
    """
    prompt = (
        f"Today is {ctx.now_line()}.\n\n"
        f"{agent_context.render()}\n\n"
        f"Reason this call was placed: {reason}\n\n"
        "Write the opening line."
    )

    completion = await client.complete(
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        speed="fast",
        max_tokens=150,
    )

    text = completion.text.strip()
    if not text:
        logger.warning("outbound opener drafting returned nothing, using a plain fallback")
        text = f"Hi, this is {agent_context.business_name} calling."

    return Opener(text=text, cost_paise=completion.cost_paise)
