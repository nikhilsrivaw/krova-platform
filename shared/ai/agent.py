"""
The agent.

Not a flow builder. A business picks its vertical at signup, which seeds how
that kind of business speaks and what it must never answer, and from then on
the agent replies from what it actually knows - the conversation, the
customer's history, and what is outstanding between them.

Three rules shape every answer:

It never invents. Prices, dates and availability come from the business's own
details or they do not appear. A wrong price quoted to a customer is worse
than no answer, because the business finds out at the counter.

It escalates honestly. An agent that answers everything is worse than one
that knows its limits. Every escalation names the gap - "paediatric pricing
isn't in your details" - and twenty of those show an owner exactly what to
fill in. The product improves by admitting what it cannot do.

It never sends by itself unless told to. The default is a draft waiting for a
person. That is the public promise, so it is enforced here rather than left
to a caller to remember.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from shared.ai import client, context as ctx
from shared.db.models import BusinessDNA
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Below this the draft is offered but flagged as uncertain, so a person reads
# it properly rather than approving on autopilot.
LOW_CONFIDENCE = 0.6

REPLY_TOOL = {
    "name": "respond",
    "description": "Decide how to handle this customer's message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["reply", "escalate", "no_action"],
                "description": (
                    "reply: you can answer from what you know. "
                    "escalate: a human is needed - always use this rather than "
                    "guessing. no_action: nothing needs a response, e.g. the "
                    "customer just said thanks."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "What to send, in the business's voice. Empty when "
                    "escalating or taking no action."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "One sentence on why you answered this way.",
            },
            "gap": {
                "type": "string",
                "description": (
                    "When escalating: exactly what you did not know, so the "
                    "owner can add it. E.g. 'paediatric treatment pricing is "
                    "not in the business details'."
                ),
            },
            "confidence": {
                "type": "number",
                "description": "0 to 1. Below 0.6 if you are unsure.",
            },
            "book_slot": {
                "type": "string",
                "description": (
                    "ISO 8601 datetime of the exact slot to book - set this only "
                    "when the business details list real availability AND the "
                    "customer has just confirmed one specific time from it. The "
                    "value must exactly match one of the times you were shown, "
                    "character for character. Omit entirely otherwise - never "
                    "set this from a time the customer merely asked about, and "
                    "never invent a time that was not offered to you."
                ),
            },
            "book_doctor": {
                "type": "string",
                "description": (
                    "The doctor's name exactly as given in the business details. "
                    "Required whenever book_slot is set and more than one doctor "
                    "was listed."
                ),
            },
            "book_property": {
                "type": "string",
                "description": (
                    "Only for a business with property listings: the property's "
                    "title exactly as given in the business details, when "
                    "book_slot is a site visit for one specific listed property "
                    "the customer has confirmed. Omit for every other business, "
                    "and omit even here if no specific property was confirmed."
                ),
            },
        },
        "required": ["action", "reasoning", "confidence"],
    },
}


SYSTEM = """You answer customer messages on behalf of a business.

You are given that business's details, what is known about this customer, \
what is outstanding between them, and the conversation so far.

Write as the business would write. Match the tone you are given. Be brief - \
these are messages, not letters. One or two sentences is usually right.

Three rules you must never break:

1. Never invent a fact. Prices, dates, availability and policies come from \
the business details you were given, or you do not state them. If a customer \
asks something the details do not cover, escalate. A wrong price is worse \
than no price.

2. Escalate honestly, and say what you did not know. When you escalate, the \
`gap` must name the missing information precisely enough that the owner can \
add it. "I could not answer" is useless; "their opening hours on Sunday are \
not in the business details" is useful.

3. Respect the rules the business gave you. If the details say never to give \
medical advice, or never to confirm a booking without checking, that holds \
even when the customer pushes.

If the business details list real current availability, that list is the \
only source of truth for booking - never a time you calculate or assume. \
When the customer has just confirmed one specific time from that list, set \
book_slot to it exactly (and book_doctor, if more than one doctor was \
listed, and book_property if this is a property viewing for one specific \
listed property) so it actually gets reserved - do this only once they \
have clearly agreed to a specific slot, not while they are still asking \
what's available.

Use what you know about the customer. If they have an outstanding payment or \
you promised them something, that is context worth using - naturally, not \
mechanically.

If the last message needs no reply - a "thanks", an emoji - choose \
no_action rather than manufacturing a response. Not every message deserves \
one, and a business that replies to everything looks automated.

Match the customer's language. If they write in Hindi, or mix Hindi and \
English the way most conversations in this market actually happen, reply \
the same way - naturally code-mixed, not forced into pure English or \
transliterated textbook Hindi. Match their script too: Devanagari gets \
Devanagari back, Roman-script Hindi gets Roman-script Hindi back."""


SYSTEM_STREAM = SYSTEM + """

This is a live phone call - the caller is waiting in silence right now, so \
every extra second you take to plan is a second they hear nothing. Output in \
exactly this format, nothing else, no markdown, no preamble:

The first line is exactly one word: REPLY, ESCALATE, or NOACTION.

If REPLY: a blank line, then the spoken reply itself - one short, natural \
sentence, the way a person would actually say it out loud, not written \
prose. Nothing after it.

If ESCALATE: a blank line, then one short phrase naming exactly what you \
did not know (five words or fewer) - not a sentence, just the missing \
fact, e.g. "paediatric treatment pricing" or "Sunday opening hours".

If NOACTION: nothing else follows.

NOACTION means only "the last thing said needs no reply at all" - a \
"thanks", a goodbye, silence. A phone call has no chat history the caller \
can scroll back through, and this conversation may show the same question \
answered several times already, across several earlier calls - that is \
completely normal here and is NOT a reason to stay quiet now. If the \
customer's most recent message is a question, answer it again, exactly as \
plainly as the first time, no matter how many times it was already \
asked or already answered earlier in this history. A caller who just \
asked something and hears nothing back assumes the line went dead. \
NOACTION is only for a message that is not a question and does not need \
a reply at all - never for a repeated question, no matter how repeated."""


# Sentence-ending punctuation a streamed reply is split on before being
# handed to TTS - good enough to start speaking a finished sentence while
# Claude is still generating the next one, without waiting for the whole
# reply or parsing anything more sophisticated than "did a sentence just end".
_SENTENCE_END = (". ", "! ", "? ", ".\n", "!\n", "?\n")


@dataclass(slots=True)
class ReplyStart:
    action: str  # "reply" | "escalate" | "no_action"


@dataclass(slots=True)
class ReplyChunk:
    text: str


@dataclass(slots=True)
class ReplyDone:
    gap: str | None
    cost_paise: int


ReplyEvent = ReplyStart | ReplyChunk | ReplyDone


async def stream_reply(agent_context: ctx.AgentContext):
    """
    The streaming counterpart to draft_reply, for a live call only.

    Text-channel drafts wait for the whole decision - action, message,
    reasoning, gap, confidence - because a person reviews it before
    anything happens. A call has no review step and a caller hearing
    silence while the model "thinks in JSON" is the entire latency problem
    this exists to fix, so this asks for plain text in a fixed, three-line
    format instead of a forced tool call, and yields the reply sentence by
    sentence as Claude generates it rather than as one block at the end.

    reasoning and confidence are dropped entirely, not because they stopped
    mattering, but because nothing downstream on a call ever reads them -
    pipeline.py's _reply() only ever used action, message and gap even
    before this existed.

    Yields ReplyStart once the action is known, then ReplyChunk per
    sentence for a "reply" action, then always ends with exactly one
    ReplyDone carrying the gap (for "escalate") and the real cost.
    """
    if not agent_context.recent:
        yield ReplyStart(action="no_action")
        yield ReplyDone(gap=None, cost_paise=0)
        return

    prompt = (
        f"Today is {ctx.now_line()}.\n\n"
        f"{agent_context.render()}\n\n"
        "Decide how to handle the customer's most recent message."
    )

    stream = client.stream_text(
        system=SYSTEM_STREAM,
        messages=[{"role": "user", "content": prompt}],
        speed="fast",
        max_tokens=300,
    )

    buffer = ""
    action: str | None = None
    gap_parts: list[str] = []
    sentence_buffer = ""

    async for delta in stream:
        buffer += delta

        if action is None:
            if "\n" not in buffer:
                continue
            first_line, _, rest = buffer.partition("\n")
            word = first_line.strip().upper()
            if word not in ("REPLY", "ESCALATE", "NOACTION"):
                logger.warning("stream_reply got unrecognised action %r, escalating", word)
                word = "ESCALATE"
            action = {"REPLY": "reply", "ESCALATE": "escalate", "NOACTION": "no_action"}[word]
            yield ReplyStart(action=action)
            # Whatever arrived after the action line and its blank line is
            # the start of the real content - route it the same way the
            # rest of the stream will be routed from here on.
            content = rest.lstrip("\n")
            if action == "reply":
                sentence_buffer = content
            elif action == "escalate" and content:
                gap_parts.append(content)
            continue

        if action == "reply":
            sentence_buffer += delta
            while True:
                cut = None
                for marker in _SENTENCE_END:
                    idx = sentence_buffer.find(marker)
                    if idx != -1 and (cut is None or idx < cut):
                        cut = idx + len(marker)
                if cut is None:
                    break
                piece = sentence_buffer[:cut].strip()
                sentence_buffer = sentence_buffer[cut:]
                if piece:
                    yield ReplyChunk(text=piece)
        elif action == "escalate":
            gap_parts.append(delta)

    if action is None:
        # The stream ended before a single newline ever arrived - a very
        # short or malformed output. Treat as escalate rather than silence.
        word = buffer.strip().upper()
        action = {"REPLY": "reply", "ESCALATE": "escalate", "NOACTION": "no_action"}.get(
            word, "escalate"
        )
        yield ReplyStart(action=action)

    if action == "reply" and sentence_buffer.strip():
        yield ReplyChunk(text=sentence_buffer.strip())

    gap = "".join(gap_parts).strip() or None if action == "escalate" else None
    yield ReplyDone(gap=gap, cost_paise=stream.cost_paise)


@dataclass(slots=True)
class Draft:
    action: str
    message: str | None
    reasoning: str
    gap: str | None
    confidence: float
    cost_paise: int
    context_message_ids: list
    # Set only when the model chose to book a real, offered slot - see
    # REPLY_TOOL. Neither is validated here; respond.py owns turning this
    # into an actual Appointment (or backing off if the slot lost a race).
    book_slot: str | None = None
    book_doctor: str | None = None
    # Which property a viewing is for - only meaningful alongside book_slot
    # for a business with property_listings. Matched by title the same way
    # book_doctor is matched by name; unset for every other vertical.
    book_property: str | None = None


async def draft_reply(agent_context: ctx.AgentContext, *, fast: bool = False) -> Draft:
    """
    Decide what to say to this customer.

    `fast` uses the low-latency model. Correct for a live call, where every
    millisecond is audible; wrong for a considered reply, where being right
    matters more than being quick.
    """
    if not agent_context.recent:
        return Draft(
            action="no_action",
            message=None,
            reasoning="No conversation to respond to.",
            gap=None,
            confidence=1.0,
            cost_paise=0,
            context_message_ids=[],
        )

    prompt = (
        f"Today is {ctx.now_line()}.\n\n"
        f"{agent_context.render()}\n\n"
        "Decide how to handle the customer's most recent message."
    )
    if fast:
        # A live caller hears every token this takes to generate, including
        # ones nobody will ever read - `reasoning` exists for the approval
        # queue's benefit, which a call has none of. Measured this alone
        # cutting total generation from ~2.5s toward first-token latency
        # (~0.78s), since most of the gap was the model writing reasoning
        # prose no one on the call would ever see.
        prompt += (
            "\n\nThis is a live phone call - the caller is waiting in silence "
            "right now. Keep 'reasoning' to five words or fewer. 'message' "
            "should be one short sentence, spoken naturally, not written."
        )

    completion = await client.complete(
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        speed="fast" if fast else "deep",
        tool=REPLY_TOOL,
        max_tokens=300 if fast else 1024,
    )

    result = completion.tool_input or {}
    action = result.get("action")
    if action not in ("reply", "escalate", "no_action"):
        # An unrecognised action means we do not know what it intended. Hand it
        # to a human rather than guessing at a customer's expense.
        logger.warning("agent returned unknown action %r, escalating", action)
        action = "escalate"

    message = (result.get("message") or "").strip() or None
    if action == "reply" and not message:
        # It chose to reply and then said nothing. Escalate rather than send
        # an empty message.
        logger.warning("agent chose reply with no message, escalating")
        action = "escalate"
        message = None

    try:
        confidence = min(1.0, max(0.0, float(result.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5

    return Draft(
        action=action,
        message=message if action == "reply" else None,
        reasoning=(result.get("reasoning") or "").strip(),
        gap=(result.get("gap") or "").strip() or None if action == "escalate" else None,
        confidence=confidence,
        cost_paise=completion.cost_paise,
        context_message_ids=agent_context.context_message_ids,
        book_slot=(result.get("book_slot") or "").strip() or None if action == "reply" else None,
        book_doctor=(result.get("book_doctor") or "").strip() or None if action == "reply" else None,
        book_property=(result.get("book_property") or "").strip() or None if action == "reply" else None,
    )


async def record_gap(business_id: uuid.UUID, gap: str, db: AsyncSession) -> None:
    """
    Remember what the agent could not answer.

    This is the column that compounds. Each escalation names something
    missing from the business's details; twenty of them tell an owner
    exactly what to add, and the agent stops failing on it. The product gets
    better by being honest about its limits rather than by guessing more
    confidently. Shared between every channel that escalates - text channels
    queue a draft for a person on top of this; voice, mid-call with nobody
    to hand off to, has only this and whatever it says out loud.
    """
    dna = await db.get(BusinessDNA, business_id)
    if dna is None:
        return

    known = dict(dna.known_gaps or {})
    learned = list(known.get("learned", []))

    # Crude dedupe - the same gap phrased slightly differently should not
    # fill the list. Good enough until it isn't.
    normalised = gap.strip().lower()
    if any(normalised[:60] in existing.lower() for existing in learned):
        return

    learned.append(gap.strip())
    known["learned"] = learned[-50:]
    dna.known_gaps = known
