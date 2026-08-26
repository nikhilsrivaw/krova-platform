"""
KROVA — Nightly Analysis Prompt
The core brain prompt. Claude reads the entire business context + every customer's
recent messages and returns structured JSON decisions for each customer.

Enhanced with:
- Layer 6: Confidence scores on every decision (Trust Layer)
- Layer 7: Owner language/communication style awareness
- Layer 8: Emotional intelligence — the business is run by a human
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CustomerContext:
    customer_id: str
    name: str | None
    phone: str | None
    email: str | None
    status: str
    health_score: int
    last_contact: str | None
    ai_notes: str | None
    recent_messages: list[dict]  # [{direction, content, channel, sent_at}]


@dataclass
class BusinessContext:
    business_id: str
    name: str
    business_type: str
    context: str | None
    good_lead_description: str | None
    lost_customer_description: str | None
    customers: list[CustomerContext]

    # Layer 1: DNA narrative — feeds richer context to Claude
    dna_narrative: str | None = None

    # Layer 7: Owner's communication style (learned over time)
    owner_language: str = "english"  # "hindi" | "english" | "hinglish"
    owner_formality: str = "casual"  # "formal" | "casual" | "mixed"

    # Layer 8: Emotional context — how is the business doing overall?
    months_below_target: int = 0  # For emotional acknowledgement
    best_month_revenue: float | None = None  # For celebrating wins


def build_nightly_prompt(ctx: BusinessContext) -> str:
    customer_blocks = "\n\n".join(
        _format_customer(c) for c in ctx.customers
    )

    dna_section = f"\nBUSINESS PERSONALITY (learned from months of data):\n{ctx.dna_narrative}\n" if ctx.dna_narrative else ""

    # Layer 8: Build emotional context for the briefing
    emotional_context = _build_emotional_context(ctx)

    return f"""You are the AI brain for KROVA — an autonomous business intelligence system for Indian SMBs.

You are analysing the business data for: {ctx.name} ({ctx.business_type})

BUSINESS CONTEXT:
{ctx.context or "No context provided."}
{dna_section}
What a GOOD LEAD looks like for this business:
{ctx.good_lead_description or "Not specified."}

What a LOST CUSTOMER looks like:
{ctx.lost_customer_description or "Not specified."}

OWNER COMMUNICATION STYLE:
Language preference: {ctx.owner_language} | Formality: {ctx.owner_formality}
(Suggested messages must sound natural in this owner's voice — not generic or robotic)

EMOTIONAL CONTEXT FOR BRIEFING:
{emotional_context}

---

CUSTOMERS AND THEIR RECENT CONVERSATIONS:

{customer_blocks}

---

INSTRUCTIONS:

Analyse every customer above. For each one, decide:
1. Their current STATUS (hot/warm/cold/converted/lost)
2. URGENCY of action needed (high/medium/low)
3. What ACTION to take (follow_up/check_in/close/nothing)
4. The exact MESSAGE to send — in the same language and tone the customer uses
5. Your REASONING in 1-2 sentences
6. Your CONFIDENCE in this decision (0.0 to 1.0) — be honest about uncertainty

CONFIDENCE RULES (Layer 6 — Trust Layer):
- < 0.4: Say "I'm not very sure — I haven't seen enough data on this type of customer yet"
- 0.4-0.6: Moderate confidence — owner should use their judgment
- 0.6-0.8: Reasonably confident — pattern is clear
- > 0.8: High confidence — act on this

IMPORTANT RULES:
- Suggested messages must sound human and personal — never robotic or templated
- Use Hindi/Hinglish if the customer writes in Hindi/Hinglish, English if they write in English
- "hot" = actively enquiring or showing strong intent in last 3 days
- "warm" = engaged but needs nurturing — last contact within 7 days
- "cold" = went quiet — no response in 7-14 days
- "lost" = no response despite follow-ups, or explicitly said not interested
- "converted" = paid or formally signed up
- Only suggest follow_up/check_in/close if the customer genuinely needs it
- The morning_briefing must be personal, conversational, and contain at least one specific surprising insight
- morning_briefing should acknowledge the emotional reality of the business (Layer 8) — celebrate wins, acknowledge tough stretches

Respond with ONLY a valid JSON object — no explanation, no markdown, no preamble:

{{
  "customers": [
    {{
      "customer_id": "<uuid>",
      "status": "hot|warm|cold|converted|lost",
      "urgency": "high|medium|low",
      "suggested_action": "follow_up|check_in|close|nothing",
      "suggested_message": "<exact message to send, or empty string if nothing>",
      "reasoning": "<1-2 sentences>",
      "confidence": 0.0
    }}
  ],
  "business_summary": {{
    "revenue_signal": "up|down|stable",
    "top_priority_today": "<one sentence — what the owner must focus on>",
    "leads_at_risk": ["<customer_id>"],
    "opportunities": ["<specific opportunity>"],
    "morning_briefing": "<warm, personal WhatsApp message — conversational, specific, with at least one insight they could not have known without KROVA>",
    "emotional_tone": "celebrating | encouraging | grounding | alert"
  }}
}}"""


def _format_customer(c: CustomerContext) -> str:
    identity_parts = []
    if c.name:
        identity_parts.append(f"Name: {c.name}")
    if c.phone:
        identity_parts.append(f"Phone: {c.phone}")
    if c.email:
        identity_parts.append(f"Email: {c.email}")

    identity = " | ".join(identity_parts) if identity_parts else "Unknown"

    messages_text = ""
    if c.recent_messages:
        lines = []
        for msg in c.recent_messages[-10:]:
            direction = "CUSTOMER" if msg.get("direction") == "inbound" else "BUSINESS"
            content = msg.get("content") or f"[{msg.get('message_type', 'media')}]"
            sent_at = msg.get("sent_at", "")[:10]
            lines.append(f"  [{sent_at}] {direction}: {content}")
        messages_text = "\n".join(lines)
    else:
        messages_text = "  No messages yet."

    ai_notes = f"\nPrevious analysis notes: {c.ai_notes}" if c.ai_notes else ""
    last_contact = f"\nLast contact: {c.last_contact[:10] if c.last_contact else 'Never'}"

    return f"""CUSTOMER ID: {c.customer_id}
{identity}
Current status: {c.status} | Health score: {c.health_score}/100{last_contact}{ai_notes}

Recent messages:
{messages_text}"""


def _build_emotional_context(ctx: BusinessContext) -> str:
    """Layer 8: Build emotional context so the morning briefing acknowledges the business owner as a human."""
    if ctx.months_below_target >= 3:
        return (
            f"This business has been under pressure for {ctx.months_below_target} months. "
            "The morning briefing should acknowledge this honestly, highlight what IS working, "
            "and focus on the most actionable path forward. Do not ignore the tough stretch."
        )
    elif ctx.months_below_target == 0 and ctx.best_month_revenue:
        return (
            "This business is performing well. If tonight's data shows strong results, "
            "the morning briefing should genuinely celebrate the win — name the number, "
            "acknowledge the effort. Business owners rarely get told 'well done.'"
        )
    else:
        return (
            "Neutral performance period. Morning briefing should be practical and specific — "
            "focus on what matters most today, not generic encouragement."
        )
