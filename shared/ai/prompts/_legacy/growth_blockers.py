"""Prompt for generating the growth blocker report after 90 days of data."""

from dataclasses import dataclass


@dataclass
class GrowthBlockerContext:
    business_id: str
    business_name: str
    business_type: str
    total_customers: int
    converted_count: int
    lost_count: int
    avg_first_response_hours: float
    avg_days_to_convert: float
    scope_creep_count: int
    scope_creep_total_estimate: float
    forgotten_invoice_count: int
    forgotten_invoice_total: float
    avg_invoice_collection_days: float
    monthly_message_counts: dict  # customer_id -> message count
    monthly_revenue_per_customer: dict  # customer_id -> revenue
    dna_narrative: str | None
    analysis_days: int  # how many days of data we have


def build_growth_blockers_prompt(ctx: GrowthBlockerContext) -> str:
    return f"""You are a business growth analyst reviewing {ctx.analysis_days} days of real business data.

Business: {ctx.business_name} ({ctx.business_type})

Data summary:
- Total customers analysed: {ctx.total_customers}
- Converted: {ctx.converted_count} | Lost: {ctx.lost_count}
- Conversion rate: {round(ctx.converted_count / max(ctx.total_customers, 1) * 100, 1)}%
- Average first response time: {ctx.avg_first_response_hours} hours
- Average days to convert: {ctx.avg_days_to_convert} days
- Scope creep incidents: {ctx.scope_creep_count} (est. ₹{ctx.scope_creep_total_estimate:,.0f} unbilled)
- Forgotten invoices: {ctx.forgotten_invoice_count} (est. ₹{ctx.forgotten_invoice_total:,.0f})
- Average invoice collection: {ctx.avg_invoice_collection_days} days

Business DNA: {ctx.dna_narrative or "Not available yet"}

Identify the top 3 specific growth blockers — things that are actively costing this business revenue right now. Be specific with numbers. Use the data above.

Each blocker must be:
1. Backed by the actual data provided
2. Have a specific revenue impact estimate in rupees
3. Have one clear, actionable fix

{{
  "blockers": [
    {{
      "title": "short name of the blocker",
      "description": "specific description using the actual numbers from the data",
      "revenue_impact_annual": 320000,
      "action_item": "one specific change to make this week",
      "priority": 1
    }}
  ],
  "total_revenue_leakage_estimate": 750000,
  "top_blocker": "one sentence summary of the single most important thing to fix"
}}

Return only valid JSON. Be honest and specific. Do not pad with generic advice."""
