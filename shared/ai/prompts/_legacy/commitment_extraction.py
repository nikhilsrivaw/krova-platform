"""Prompt for extracting commitments from messages."""

from dataclasses import dataclass


@dataclass
class CommitmentExtractionContext:
    business_id: str
    customer_name: str | None
    customer_id: str
    messages: list[dict]  # [{direction, content, channel, sent_at}]


def build_commitment_extraction_prompt(ctx: CommitmentExtractionContext) -> str:
    messages_text = "\n".join([
        f"[{m['sent_at']} | {m['direction'].upper()} | {m['channel']}]: {m['content']}"
        for m in ctx.messages[-30:]  # Last 30 messages only
        if m.get("content")
    ])

    return f"""You are extracting commitments and promises from a business conversation.

Customer: {ctx.customer_name or "Unknown"}

Conversation:
{messages_text}

Extract every commitment or promise made by the BUSINESS OWNER (outbound messages) that has not yet been fulfilled based on the conversation.

Look for:
- "I will send you..."
- "I'll get back to you by..."
- "Let me introduce you to..."
- "I'll share the proposal by..."
- "I'll call you on..."
- "Will send the invoice by..."
- Any promise or commitment to do something

For each commitment found, output JSON:
{{
  "commitments": [
    {{
      "commitment_text": "exact description of what was promised",
      "due_date": "YYYY-MM-DD if mentioned, null if not",
      "is_likely_fulfilled": false,
      "evidence": "the message text that contains the commitment"
    }}
  ]
}}

If no commitments found, return: {{"commitments": []}}

Return only valid JSON. No explanation."""
