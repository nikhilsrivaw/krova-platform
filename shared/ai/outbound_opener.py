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


# The most a real opener can be before it stops looking like one - see
# draft()'s own fallback for why this matters.
_MAX_OPENER_CHARS = 400


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
    # A real opener is "one or two short sentences" per SYSTEM's own
    # instruction - well under this. Caught on a real test call: given a
    # meaningless reason string and near-empty business context, the model
    # sometimes declines to draft anything and writes a multi-paragraph
    # explanation of why it won't ("I can't complete this request...")
    # instead - which, with no check here, got spoken aloud to the person
    # who picked up, verbatim. A generic, honest fallback is always better
    # than reading the model's own reasoning to a real caller.
    if not text or len(text) > _MAX_OPENER_CHARS:
        if text:
            logger.warning(
                "outbound opener drafting returned something opener-shaped text "
                "isn't (%d chars) - likely a decline/explanation, using a plain fallback",
                len(text),
            )
        else:
            logger.warning("outbound opener drafting returned nothing, using a plain fallback")
        text = f"Hi, this is {agent_context.business_name} calling."

    return Opener(text=text, cost_paise=completion.cost_paise)


# Same split points agent.py's own streamed replies use - "did a sentence
# just end" is all TTS needs to start speaking one while the next is still
# being written.
_SENTENCE_END = (". ", "! ", "? ", ".\n", "!\n", "?\n")


async def draft_stream(
    agent_context: ctx.AgentContext, *, reason: str, cost_sink: dict | None = None,
):
    """
    draft()'s streaming twin, for the one place a person is listening to
    silence while this runs: the opening line of a live outbound call.

    draft() waits for Claude's whole response before TTS sees a single
    character, so the caller waits out the full generation (measured
    around 1.5-2s) before synthesis has even started. This yields each
    finished sentence as it arrives instead, so the first one reaches
    Sarvam while the rest is still being written - the same trick
    agent.py's stream_reply already uses for every other spoken turn, and
    the reason a reply mid-call feels quicker than the opener that
    precedes it did.

    Yields spoken text pieces. `cost_sink`, when given, has its "paise"
    key set once the stream is fully consumed - the same shape
    pipeline.py's own reply_cost dict uses, since an async generator
    cannot hand back a value alongside its yields and this call still has
    to reach the per-tenant cost ledger like every other.
    """
    prompt = (
        f"Today is {ctx.now_line()}.\n\n"
        f"{agent_context.render()}\n\n"
        f"Reason this call was placed: {reason}\n\n"
        "Write the opening line."
    )

    stream = client.stream_text(
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        speed="fast",
        max_tokens=150,
    )

    buffer = ""
    spoken = 0
    async for delta in stream:
        buffer += delta
        while True:
            cut = -1
            for marker in _SENTENCE_END:
                idx = buffer.find(marker)
                if idx != -1 and (cut == -1 or idx < cut):
                    cut = idx + len(marker)
            if cut == -1:
                break
            piece = buffer[:cut].strip()
            buffer = buffer[cut:]
            if piece:
                spoken += len(piece)
                # Same guard draft() applies to a whole response, enforced
                # as it streams: a decline/explanation is far longer than
                # any real opener, and must never reach the caller's ear.
                if spoken > _MAX_OPENER_CHARS:
                    logger.warning(
                        "outbound opener stream ran past %d chars - likely a "
                        "decline/explanation, cutting it off",
                        _MAX_OPENER_CHARS,
                    )
                    return
                yield piece

    tail = buffer.strip()
    if tail and spoken + len(tail) <= _MAX_OPENER_CHARS:
        yield tail

    if cost_sink is not None:
        cost_sink["paise"] = stream.cost_paise
