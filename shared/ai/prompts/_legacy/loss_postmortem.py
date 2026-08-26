"""
KROVA — Loss Post-Mortem Prompt
When a lead moves to 'lost', Claude analyzes the full conversation thread
to produce a structured explanation of why it was lost and what could have been done differently.

This builds KROVA's understanding of the business's specific loss patterns over time.
Output is stored in the customer's ai_notes and surfaced in the intelligence layer.
"""

from dataclasses import dataclass


@dataclass
class PostmortemContext:
    customer_name: str | None
    business_type: str
    conversation_summary: str   # Last 30 messages concatenated
    final_status: str           # lost
    health_score_at_loss: int
    commitments_missed: list[str]  # Unfulfilled commitments at time of loss
    days_in_pipeline: int


def build_loss_postmortem_prompt(ctx: PostmortemContext) -> str:
    missed = "\n".join(f"- {c}" for c in ctx.commitments_missed) if ctx.commitments_missed else "None detected"

    return f"""You are KROVA's loss analyst. A lead has been marked as LOST.
Analyze the conversation and produce a structured post-mortem.

Business type: {ctx.business_type}
Customer: {ctx.customer_name or "Unknown"}
Days in pipeline before loss: {ctx.days_in_pipeline}
Health score at loss: {ctx.health_score_at_loss}/100
Unfulfilled commitments at time of loss:
{missed}

CONVERSATION THREAD (most recent last):
{ctx.conversation_summary}

Analyze this loss and respond with a JSON object:
{{
  "primary_reason": "one-sentence root cause",
  "category": "price | competitor | timing | trust | no_response | our_delay | unclear_value | other",
  "warning_signs": ["list of 2-3 signals that appeared before the loss"],
  "what_we_missed": "specific missed opportunity — max 2 sentences",
  "what_to_do_differently": "concrete action for the next similar lead — max 2 sentences",
  "recovery_possible": true or false,
  "recovery_action": "if recovery_possible, what to try — otherwise null",
  "confidence": 0.0 to 1.0
}}

Respond only with valid JSON."""


def parse_postmortem_response(raw: str) -> dict:
    import json, re
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception:
        pass
    return {
        "primary_reason": "Could not analyze",
        "category": "other",
        "warning_signs": [],
        "what_we_missed": None,
        "what_to_do_differently": None,
        "recovery_possible": False,
        "recovery_action": None,
        "confidence": 0.0,
    }
