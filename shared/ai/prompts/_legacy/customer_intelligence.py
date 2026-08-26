"""
KROVA — Customer Intelligence Prompt (Layer 3: Relationship Intelligence Layer)
Builds a deep relationship profile for one customer from their conversation history.
After 100+ interactions, KROVA understands this customer better than the owner's memory.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CustomerIntelligenceContext:
    customer_id: str
    customer_name: str | None
    customer_phone: str | None
    primary_channel: str
    current_status: str
    health_score: int
    days_since_first_contact: int
    interaction_count: int

    # Existing intelligence (update, don't replace)
    existing_profile: dict

    # Message history (all messages, not just last 10)
    messages: list[dict]  # [{direction, content, channel, sent_at}]

    # Action outcomes for this customer
    action_outcomes: list[dict]  # [{action_type, message, outcome, sent_at}]

    # Business context
    business_type: str
    dna_narrative: str | None


def build_customer_intelligence_prompt(ctx: CustomerIntelligenceContext) -> str:
    existing = str(ctx.existing_profile) if ctx.existing_profile else "No profile yet."

    messages_block = "\n".join(
        f"  [{m.get('sent_at', '')[:10]}] {'CUSTOMER' if m.get('direction') == 'inbound' else 'BUSINESS'}: {m.get('content', '[media]')}"
        for m in ctx.messages[-30:]  # Last 30 messages
    )

    outcomes_block = "\n".join(
        f"  [{o.get('sent_at', '')[:10]}] Sent: '{o.get('message', '')[:80]}' → Outcome: {o.get('outcome', 'pending')}"
        for o in ctx.action_outcomes[-10:]
    )

    return f"""You are building a deep relationship intelligence profile for one customer.

BUSINESS TYPE: {ctx.business_type}
BUSINESS PERSONALITY: {ctx.dna_narrative or "Not available."}

CUSTOMER: {ctx.customer_name or "Unknown"} | {ctx.customer_phone or "No phone"}
Channel: {ctx.primary_channel} | Status: {ctx.current_status} | Health: {ctx.health_score}/100
Days since first contact: {ctx.days_since_first_contact} | Total interactions: {ctx.interaction_count}

EXISTING PROFILE (refine this, don't replace):
{existing}

FULL CONVERSATION HISTORY:
{messages_block or "No messages yet."}

ACTION OUTCOMES (what worked / didn't work):
{outcomes_block or "No actions yet."}

---

TASK:
Build or refine the relationship intelligence profile for this customer.

Look for:
1. communication: What language do they use? Formal or casual? Which days/times do they reply fastest? Do they prefer voice notes?
2. decision_making: Fast or slow decider? Who else influences their decision (spouse, business partner)? How many days does it usually take them to decide?
3. objection_patterns: What objections do they raise? When in the conversation? How do they raise them?
4. conversion_triggers: What has worked in the past? What moved them closer to converting?
5. relationship_health: Is sentiment improving, stable, or declining? How often do they engage?
6. value_signals: What's their estimated lifetime value? Are they likely to refer? Are they ready for an upsell?

Then write:
- current_recommendation: Given their current situation, what should the owner say or do RIGHT NOW?
- message_template: A personalised message template for this specific customer's style and situation

CONFIDENCE RULES:
- < 10 interactions: 0.2-0.3 (limited data)
- 10-30 interactions: 0.4-0.6 (forming patterns)
- 30+ interactions: 0.7-0.9 (reliable intelligence)

Respond with ONLY valid JSON:

{{
  "profile": {{
    "communication": {{
      "language": "hindi | english | hinglish",
      "formality": "formal | casual",
      "preferred_channel": "",
      "best_contact_days": [],
      "best_contact_time": "",
      "avg_response_time_hours": null,
      "prefers_voice_notes": false
    }},
    "decision_making": {{
      "style": "fast | slow | needs_approval",
      "influencers": [],
      "avg_days_to_decide": null,
      "requires_demo": false
    }},
    "objection_patterns": [],
    "conversion_triggers": [],
    "relationship_health": {{
      "sentiment_trend": "improving | stable | declining",
      "last_sentiment": "positive | neutral | negative",
      "trust_level": "high | medium | low",
      "engagement_frequency": "weekly | monthly | sporadic"
    }},
    "value_signals": {{
      "estimated_ltv": null,
      "referral_potential": "high | medium | low",
      "upsell_readiness": "ready | not_yet | unlikely",
      "payment_behaviour": "fast | average | slow"
    }},
    "interaction_count": {ctx.interaction_count},
    "confidence": 0.0,
    "last_updated": ""
  }},
  "current_recommendation": "<what owner should do RIGHT NOW with this specific customer>",
  "message_template": "<personalised message for this customer's style — use {{customer_name}} as placeholder>"
}}"""
