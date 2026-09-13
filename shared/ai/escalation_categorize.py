"""
Classifying an escalation's own free-text reason into a small, fixed
category - one field that unlocks a badge on /escalations, an Automations
condition field, and (real, separate follow-ups, not built here yet)
routing and analytics, all "for free" once it exists.

Deliberately never called from shared/ai/agent.py::notify_escalation()
itself - that function runs on live voice turns and live chat replies,
exactly the paths this codebase's own voice-latency work spent real
effort keeping fast. This module is only ever called from
shared/care/escalation_failsafe.py's own periodic sweep, off the hot
path entirely - see that module's own docstring.
"""

from dataclasses import dataclass

from shared.ai import client
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Fixed and small on purpose - a category is only useful for filtering,
# routing and analytics if there are few enough of them to mean
# something. "urgent" is separate from severity elsewhere in this
# codebase (Insight.severity) - this is about WHAT the escalation is
# about, not how bad it is.
CATEGORIES = ("billing", "booking", "complaint", "technical", "urgent", "other")

SYSTEM = f"""You classify why a business's AI agent escalated a \
conversation to a human, based on the reason it gave.

Respond with EXACTLY ONE of these words, nothing else - no punctuation, \
no explanation, just the single word:

{", ".join(CATEGORIES)}

billing = payments, refunds, pricing, invoices
booking = appointments, scheduling, slots, orders, bookings
complaint = the customer is unhappy or frustrated about something already \
done
technical = the agent itself failed, misunderstood, or couldn't do \
something it should be able to
urgent = time-sensitive or safety-relevant, needs a human right now \
regardless of topic
other = doesn't clearly fit any of the above

If more than one could apply, pick the one that best explains why a human \
specifically is needed."""


@dataclass(slots=True)
class Category:
    category: str
    cost_paise: int


async def categorize(reason: str) -> Category:
    """One escalation's reason, classified. Never raises - an unexpected
    or malformed model response falls back to "other" rather than storing
    something a filter/condition field couldn't match against."""
    completion = await client.complete(
        system=SYSTEM,
        messages=[{"role": "user", "content": reason}],
        speed="fast",
        max_tokens=20,
    )
    raw = completion.text.strip().lower()
    if raw not in CATEGORIES:
        logger.info("escalation categorize: unexpected model output %r, defaulting to other", raw)
        return Category(category="other", cost_paise=completion.cost_paise)
    return Category(category=raw, cost_paise=completion.cost_paise)
