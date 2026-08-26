"""Prompt for extracting competitor mentions from messages."""

from dataclasses import dataclass


@dataclass
class CompetitorExtractionContext:
    business_id: str
    customer_name: str | None
    customer_id: str
    messages: list[dict]


def build_competitor_extraction_prompt(ctx: CompetitorExtractionContext) -> str:
    messages_text = "\n".join([
        f"[{m['sent_at']} | {m['direction'].upper()} | {m['channel']}]: {m['content']}"
        for m in ctx.messages[-50:]
        if m.get("content")
    ])

    return f"""You are extracting competitor mentions from a business conversation.

Customer: {ctx.customer_name or "Unknown"}

Conversation:
{messages_text}

Find every mention of a competitor, alternative service, or other provider the customer references.

For each mention:
{{
  "competitor_mentions": [
    {{
      "competitor_name": "name of the competitor or service",
      "sentiment": "comparing | switched | neutral",
      "context": "brief description of how they mentioned it",
      "channel": "whatsapp | gmail | instagram",
      "evidence": "the exact message text"
    }}
  ]
}}

sentiment definitions:
- comparing: customer is actively comparing this business with competitor
- switched: customer indicates they moved some or all work to competitor
- neutral: passing mention, no direct comparison

If no competitors mentioned: {{"competitor_mentions": []}}

Return only valid JSON."""
