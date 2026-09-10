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
  2. The vertical template's escalate_immediately list. This is NOT
     business-configurable and can't be disabled from the auto-send rules
     UI - a clinic's "severe pain, bleeding, or an emergency" escalates
     regardless of how much a business otherwise trusts the agent, exactly
     as clinic.json's own comment already promises elsewhere in the
     prompt. Checked against both the customer's inbound message (which is
     what should have triggered escalation in the first place) and the
     reply itself.

Deliberately coarse: this is a substring match against short phrases, not
semantic understanding of either message - it will miss a paraphrase of
"severe pain" that never uses those words. That's a known, accepted
tradeoff for this first version (see docs/conditional-autonomy.md's own
"deterministic check granularity" question) - it still catches the exact
cases the template authors already wrote down, at zero latency and zero
extra model cost, and a missed catch here still leaves the draft exactly
as safe as ordinary `draft` mode - it just doesn't get the speed benefit.
An LLM-based semantic fallback for the cases this misses is real future
work, not something to fake with a bigger keyword list.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class GateResult:
    allowed: bool
    reason: str | None = None


DEFAULT_MIN_CONFIDENCE = 0.85


def _contains_any(text: str, phrases: list[str]) -> str | None:
    lowered = text.lower()
    for phrase in phrases:
        needle = phrase.strip().lower()
        if needle and needle in lowered:
            return phrase
    return None


def check(
    *,
    reply_body: str,
    inbound_text: str,
    confidence: float,
    auto_send_rules: dict,
    escalate_immediately: list[str],
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
    hit = _contains_any(inbound_text, escalate_immediately) or _contains_any(
        reply_body, escalate_immediately
    )
    if hit:
        return GateResult(allowed=False, reason=f"touches an always-escalate topic: {hit!r}")

    return GateResult(allowed=True)
