"""
KROVA — Business DNA Prompt (Layer 1: Memory Layer)
Builds and refines the evolving personality profile of a business.
Called after every nightly analysis. Gets smarter with every run.
After 90 days the profile is so specific it cannot exist anywhere else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class DNAUpdateContext:
    business_id: str
    business_name: str
    business_type: str
    existing_profile: dict  # The current DNA profile (empty dict on first run)
    analysis_count: int     # How many times we've updated this profile before

    # This week's data
    customers_analysed: int
    conversions_this_week: int
    losses_this_week: int
    new_leads_this_week: int

    # Pattern data extracted from tonight's analysis
    customer_summaries: list[dict]  # [{name, status, channel, last_action, outcome, reasoning}]
    action_outcomes: list[dict]     # [{action_type, channel, outcome}]

    # Context from the owner
    business_context: str | None
    good_lead_description: str | None
    lost_customer_description: str | None


def build_dna_update_prompt(ctx: DNAUpdateContext) -> str:
    existing = json.dumps(ctx.existing_profile, indent=2) if ctx.existing_profile else "No profile yet — this is the first analysis."

    customer_block = "\n".join(
        f"- {c.get('name', 'Unknown')} | Status: {c.get('status')} | Channel: {c.get('channel')} | "
        f"Reasoning: {c.get('reasoning', '')}"
        for c in ctx.customer_summaries[:50]  # Cap at 50 for prompt size
    )

    outcome_block = "\n".join(
        f"- {o.get('action_type')} via {o.get('channel')}: outcome = {o.get('outcome', 'pending')}"
        for o in ctx.action_outcomes[:30]
    )

    return f"""You are building the Business DNA profile for KROVA — an evolving personality model of a business.

BUSINESS: {ctx.business_name} ({ctx.business_type})
ANALYSIS RUN NUMBER: {ctx.analysis_count + 1}

OWNER-PROVIDED CONTEXT:
{ctx.business_context or "Not provided."}

What a good lead looks like (owner's words):
{ctx.good_lead_description or "Not specified."}

What losing a customer looks like (owner's words):
{ctx.lost_customer_description or "Not specified."}

---

EXISTING DNA PROFILE (update this, do not replace it entirely):
{existing}

---

THIS WEEK'S DATA:
- Customers analysed: {ctx.customers_analysed}
- New leads: {ctx.new_leads_this_week}
- Conversions: {ctx.conversions_this_week}
- Losses: {ctx.losses_this_week}

Customer status breakdown:
{customer_block or "No customers this week."}

Action outcomes:
{outcome_block or "No actions recorded this week."}

---

TASK:
Update the Business DNA profile by incorporating this week's data into the existing profile.

The profile must capture:
1. communication_style: How does this business naturally communicate? Language (hindi/english/hinglish), formality, tone, signature phrases observed in conversations.
2. conversion_patterns: What types of clients convert? What triggers the conversion? Average days to close? Win rate?
3. problem_patterns: What types of clients cause problems, churn, or demand too much? Common objections? Red flags?
4. seasonal_patterns: Any patterns in timing? Which months seem strong vs slow?
5. channel_insights: Which channel produces the best leads? Which converts best? Observed response time patterns.
6. pricing_intelligence: Observed deal sizes, pricing objections, discount patterns, payment preferences.
7. relationship_insights: Any referral patterns? Network density?

RULES:
- If existing_profile has data, refine it — don't overwrite patterns that took weeks to establish
- Only add new observations from THIS WEEK's data
- Be specific, not generic — "education sector clients take longer to decide" not "some clients are slow"
- Increase confidence as analysis_count grows — after 12+ runs, patterns are reliable
- If there's not enough data to fill a field, leave it with a note like "insufficient data"
- narrative: Write 2-3 sentences describing what makes this business unique — used in Claude prompts

Respond with ONLY a valid JSON object:

{{
  "profile": {{
    "communication_style": {{
      "language": "hindi | english | hinglish",
      "formality": "casual | formal | mixed",
      "tone": "warm | professional | direct",
      "signature_phrases": []
    }},
    "conversion_patterns": {{
      "best_client_segments": [],
      "avg_days_to_convert": null,
      "conversion_triggers": [],
      "win_rate": null
    }},
    "problem_patterns": {{
      "high_churn_segments": [],
      "common_objections": [],
      "red_flags": []
    }},
    "seasonal_patterns": {{
      "best_months": [],
      "slow_months": [],
      "insights": ""
    }},
    "channel_insights": {{
      "best_for_leads": "",
      "best_for_conversion": "",
      "avg_response_time_hours": {{}}
    }},
    "pricing_intelligence": {{
      "avg_deal_size": null,
      "min_deal": null,
      "max_deal": null,
      "discount_effectiveness": "",
      "payment_preferences": ""
    }},
    "relationship_insights": {{
      "referral_rate": null,
      "network_density": "low | medium | high"
    }},
    "confidence": 0.0,
    "data_points_analyzed": {ctx.customers_analysed},
    "weeks_of_data": {max(1, ctx.analysis_count // 7)},
    "last_updated": ""
  }},
  "narrative": "<2-3 sentences describing what makes this business unique>"
}}"""
