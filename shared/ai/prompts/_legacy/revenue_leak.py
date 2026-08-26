"""Prompt for detecting revenue leaks from messages."""

from dataclasses import dataclass


@dataclass
class RevenueLeakContext:
    business_id: str
    customer_name: str | None
    customer_id: str
    messages: list[dict]
    revenue_entries: list[dict]  # existing logged payments for this customer
    business_type: str


def build_revenue_leak_prompt(ctx: RevenueLeakContext) -> str:
    messages_text = "\n".join([
        f"[{m['sent_at']} | {m['direction'].upper()} | {m['channel']}]: {m['content']}"
        for m in ctx.messages[-50:]
        if m.get("content")
    ])

    revenue_text = "\n".join([
        f"- {r.get('status', 'unknown')}: ₹{r.get('amount', 0)} on {r.get('payment_date') or r.get('due_date', 'unknown')} — {r.get('description', '')}"
        for r in ctx.revenue_entries
    ]) or "No revenue entries logged."

    return f"""You are a revenue analyst reviewing a business conversation for unbilled or at-risk revenue.

Business type: {ctx.business_type}
Customer: {ctx.customer_name or "Unknown"}

Known payments/invoices:
{revenue_text}

Conversation:
{messages_text}

Identify any revenue leaks — money the business should be earning but isn't.

Types to look for:
- scope_creep: customer asked for additional work not in original agreement that was done but not billed
- forgotten_invoice: project was completed but payment conversation never happened
- ghost_invoice: invoice was sent, customer is active and responding, but payment topic is being avoided
- retainer_mismatch: evidence that business is doing significantly more work than the retainer covers

For each signal found:
{{
  "revenue_signals": [
    {{
      "signal_type": "scope_creep | forgotten_invoice | ghost_invoice | retainer_mismatch",
      "estimated_amount": 15000,
      "description": "plain language description of the leak",
      "evidence": "the specific message or pattern that indicates this"
    }}
  ]
}}

Only flag real signals with clear evidence. Do not speculate.
If no leaks found: {{"revenue_signals": []}}

Return only valid JSON."""
