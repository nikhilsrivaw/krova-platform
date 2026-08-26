"""Prompt for generating pre-call context brief for a customer."""

from dataclasses import dataclass


@dataclass
class ContextBriefContext:
    customer_name: str | None
    customer_id: str
    business_type: str
    messages: list[dict]
    revenue_entries: list[dict]
    commitments: list[dict]
    intelligence_profile: dict
    current_status: str
    health_score: int
    relationship_trajectory: str | None
    churn_probability: float | None


def build_context_brief_prompt(ctx: ContextBriefContext) -> str:
    messages_text = "\n".join([
        f"[{m['sent_at']} | {m['direction'].upper()}]: {m['content']}"
        for m in ctx.messages[-20:]
        if m.get("content")
    ])

    commitments_text = "\n".join([
        f"- {c['commitment_text']} (due: {c.get('due_date', 'unspecified')})"
        for c in ctx.commitments
        if not c.get("is_fulfilled")
    ]) or "None outstanding"

    revenue_text = "\n".join([
        f"- {r.get('status')}: ₹{r.get('amount')} — {r.get('description', '')}"
        for r in ctx.revenue_entries[-5:]
    ]) or "No revenue records"

    return f"""You are preparing a pre-call briefing for a business owner who is about to contact a customer.

Customer: {ctx.customer_name or "Unknown"}
Status: {ctx.current_status} | Health: {ctx.health_score}/100
Trajectory: {ctx.relationship_trajectory or "unknown"} | Churn probability: {f"{round((ctx.churn_probability or 0) * 100)}%" if ctx.churn_probability else "unknown"}

Outstanding commitments:
{commitments_text}

Financial history:
{revenue_text}

Recent conversation:
{messages_text}

Generate a brief the owner can read in 30 seconds before calling or messaging this customer.

{{
  "brief": "2-4 sentence plain language summary of who this person is, where the relationship stands, and what to keep in mind",
  "opening_suggestion": "one specific suggested opening line for the conversation",
  "avoid": "one thing to NOT say or do in this conversation",
  "priority_action": "the single most important thing to accomplish in this interaction"
}}

Be specific. Reference actual details from the conversation. No generic advice.
Return only valid JSON."""
