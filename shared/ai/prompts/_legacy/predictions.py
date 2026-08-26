"""
KROVA — Predictions Prompt (Layer 2: Prediction Layer)
Generates specific, falsifiable predictions about customer behaviour.
Confidence scores are shown to owners — honest about uncertainty.
Predictions get more accurate over time as they're validated against outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PredictionContext:
    business_id: str
    business_name: str
    business_type: str
    dna_narrative: str | None  # Business DNA narrative from Layer 1

    # Customers with their current signals
    customers: list[dict]  # [{id, name, status, health_score, days_since_contact,
                           #   health_trend, message_frequency_trend, reasoning}]

    # Historical patterns (from DNA profile)
    avg_days_to_convert: float | None
    common_churn_patterns: list[str]
    conversion_triggers: list[str]

    # Business metrics
    total_pipeline_value_estimate: float | None
    current_month_conversions: int


def build_predictions_prompt(ctx: PredictionContext) -> str:
    dna_context = ctx.dna_narrative or "No DNA profile yet — use general patterns."

    customer_signals = "\n".join(
        f"- ID: {c['id']} | {c.get('name', 'Unknown')} | Status: {c['status']} | "
        f"Health: {c['health_score']}/100 | Days silent: {c.get('days_since_contact', '?')} | "
        f"Health trend: {c.get('health_trend', 'stable')} | "
        f"Notes: {c.get('reasoning', '')}"
        for c in ctx.customers
    )

    churn_patterns = ", ".join(ctx.common_churn_patterns) if ctx.common_churn_patterns else "unknown yet"
    triggers = ", ".join(ctx.conversion_triggers) if ctx.conversion_triggers else "unknown yet"

    return f"""You are the prediction engine for KROVA — generating specific, falsifiable predictions about customer behaviour.

BUSINESS: {ctx.business_name} ({ctx.business_type})

BUSINESS PERSONALITY (DNA):
{dna_context}

KNOWN PATTERNS FOR THIS BUSINESS:
- Avg days to convert: {ctx.avg_days_to_convert or "unknown"}
- Common churn patterns: {churn_patterns}
- Conversion triggers: {triggers}

CURRENT CUSTOMER SIGNALS:
{customer_signals or "No customers to analyse."}

---

TASK:
Generate predictions for customers that show clear signals. Only generate predictions where you have enough evidence — do not predict for every customer.

For each prediction:
- Be SPECIFIC: "78% chance Rahul goes cold in 3 days" not "Rahul might disengage"
- Include CONFIDENCE: how sure are you of this probability? (based on data quality)
- Show EVIDENCE: what signals are you reading?
- Suggest SPECIFIC ACTION: exactly what the owner should do

Prediction types:
- churn_risk: Customer is about to go cold or leave (health dropping, silence growing)
- conversion_window: Customer is ready to buy — act now or miss the window
- upsell_opportunity: Existing customer ready for a larger engagement
- reactivation: Cold lead showing signs of life (opened something, replied to old message)
- revenue_at_risk: Business-level — significant pipeline going cold (omit customer_id)
- referral_likely: This happy customer will probably refer someone if nudged

CONFIDENCE RULES (be honest):
- < 0.4: Not enough data — mention this to owner
- 0.4-0.6: Pattern match but uncertain
- 0.6-0.8: Strong signal, reasonable confidence
- > 0.8: Very clear pattern — owner should act immediately

Respond with ONLY valid JSON:

{{
  "predictions": [
    {{
      "customer_id": "<uuid or null for business-level>",
      "prediction_type": "churn_risk | conversion_window | upsell_opportunity | reactivation | revenue_at_risk | referral_likely",
      "probability": 0.82,
      "confidence": 0.75,
      "prediction_text": "<specific, plain language prediction>",
      "recommended_action": "<exactly what to do>",
      "predicted_for_date": "<YYYY-MM-DD or null>",
      "evidence": {{
        "signals": ["health score dropped 22 points in 3 days", "no reply to last 2 messages"],
        "pattern_match": "similar to 3 previous losses in same segment"
      }}
    }}
  ]
}}"""
