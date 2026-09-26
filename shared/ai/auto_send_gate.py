"""
Deciding whether a drafted reply is safe to send without a person - the
mechanism `conditional` autonomy actually depends on.

The one thing this file is careful never to do: let the same model that
wrote the reply also grade whether the reply is safe. `Draft.confidence`
(shared/ai/agent.py) is already the model's own self-assessment - reusing
it as a send/no-send gate would just be the model grading its own
homework a second time, not a real safety check. Everything here is a
deterministic, independently-computed check instead.

Two layers, in order, and either one blocking is enough to hold the
draft back:

  1. A business's own configured rule (Business.settings["auto_send_rules"])
     - a minimum confidence floor and a list of words/phrases they never
     want sent unsupervised. Theirs to set, theirs to loosen. Checked
     against the reply about to be sent.
  2. The always-escalate floor: UNIVERSAL_ESCALATE_KEYWORDS below, plus
     the vertical template's own `escalate_keywords`. This is NOT
     business-configurable and can't be disabled from the auto-send rules
     UI - a clinic's "bleeding" or anyone's "refund" escalates regardless
     of how much a business otherwise trusts the agent. Checked against
     both the customer's inbound message (what should have triggered
     escalation in the first place) and the reply itself.

Why keywords and not the template's `escalate_immediately` list: that list
is written as descriptive sentences for the model to read ("Any dispute
about a fee already paid, or a refund request"). Until 2026-09-25 this
gate substring-matched those sentences, which no customer ever types - so
the floor never fired for any business on any template. Tested: "Mujhe
refund chahiye, fees wapas karo" passed straight through. The sentences
stay where they work (the agent's prompt, shared/ai/context.py); this gate
now reads short literal phrases in English, Hinglish and Hindi, which is
what customers actually write.

Deliberately coarse: substring match, not understanding. It will miss a
paraphrase nobody listed. And unlike what an earlier version of this
docstring said, a miss here is NOT as safe as draft mode - in conditional
mode a draft that clears this gate is sent unsupervised. That is why the
lists lean towards over-matching: a false positive only holds a draft for
a human (exactly draft mode), while a false negative sends it. An LLM
classifier for what the phrases miss is the planned next layer
(docs/conditional-autonomy.md), not something to fake with an ever-longer
list.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class GateResult:
    allowed: bool
    reason: str | None = None


DEFAULT_MIN_CONFIDENCE = 0.85


# Checked for every business, whatever its template - money disputes,
# legal threats and fraud accusations never go out unsupervised in any
# industry. Template-specific phrases live in each template's own
# `escalate_keywords` (shared/verticals/templates/*.json).
#
# Written as customers write: English, romanised Hindi (several common
# spellings) and Devanagari. Multi-word where a single word would fire on
# ordinary chat. Matching is on lowercased text with whitespace collapsed,
# so "Paise  WAPAS" still matches.
UNIVERSAL_ESCALATE_KEYWORDS: tuple[str, ...] = (
    # money back
    "refund", "money back", "paise wapas", "paisa wapas", "paise vapas", "paisa vapas",
    "fees wapas", "fee wapas", "fees vapas", "amount wapas", "return my money",
    "chargeback", "रिफंड", "पैसे वापस", "पैसा वापस",
    # legal / authority
    "consumer court", "consumer forum", "legal notice", "legal action", "lawyer", "advocate",
    "police", "fir darj", "fir file", "file an fir", "file fir", "court case", "court mein",
    "court me ", "court jaunga", "court jayenge", "take you to court", "वकील", "पुलिस",
    # fraud accusations. Not "cheat" alone - a gym's "cheat day".
    "fraud", "scam", "cheated", "cheating", "dhokha", "dhoka", "thagi", "loot liya",
    "धोखा", "ठगी",
    # formal complaint
    "complaint", "shikayat", "shikaayat", "शिकायत",
)


def _normalise(text: str) -> str:
    return " ".join(text.lower().split()) + " "


def _contains_any(text: str, phrases) -> str | None:
    """
    The first phrase found in `text`, or None.

    A trailing space is appended to the normalised text so a phrase written
    with its own trailing space ("fir ") matches at the very end of a
    message too - how a short word is kept from firing inside a longer one
    ("fir " must not match "firm").
    """
    lowered = _normalise(text)
    for phrase in phrases:
        needle = " ".join(phrase.lower().split())
        if phrase.endswith(" "):
            needle += " "
        if needle.strip() and needle in lowered:
            return phrase.strip()
    return None


def check(
    *,
    reply_body: str,
    inbound_text: str,
    confidence: float,
    auto_send_rules: dict,
    escalate_keywords: list[str],
) -> GateResult:
    """
    Pure and synchronous on purpose - no DB, no model call, nothing that
    can be slow or flaky on the hot path a customer is waiting on.
    """
    if not auto_send_rules.get("enabled"):
        return GateResult(allowed=False, reason="auto-send not enabled for this business")

    min_confidence = auto_send_rules.get("min_confidence", DEFAULT_MIN_CONFIDENCE)
    if confidence < min_confidence:
        return GateResult(
            allowed=False,
            reason=f"confidence {confidence:.2f} below the {min_confidence:.2f} floor",
        )

    blocked = auto_send_rules.get("blocked_keywords") or []
    hit = _contains_any(reply_body, blocked)
    if hit:
        return GateResult(allowed=False, reason=f"reply matches blocked phrase: {hit!r}")

    # Hard floor - not something a business's own rule can loosen. Checked
    # against what the customer actually said (what should have triggered
    # escalation) as well as the reply itself.
    floor = (*UNIVERSAL_ESCALATE_KEYWORDS, *escalate_keywords)
    hit = _contains_any(inbound_text, floor) or _contains_any(reply_body, floor)
    if hit:
        return GateResult(allowed=False, reason=f"touches an always-escalate topic: {hit!r}")

    return GateResult(allowed=True)
