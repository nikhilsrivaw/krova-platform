"""
KROVA — Weekly Insight Prompt (Layer 12: Learning Layer)
Generates one specific, data-derived lesson per business per week.
Never a generic tip. Always specific to this business's data and patterns.
Week after week the owner becomes measurably better at running their business.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WeeklyInsightContext:
    business_id: str
    business_name: str
    business_type: str

    # This week's performance data
    week: str  # ISO week e.g. "2026-W15"

    # Metrics this week vs last week
    new_leads_this_week: int
    new_leads_last_week: int
    conversions_this_week: int
    conversions_last_week: int
    reply_rate_this_week: float
    reply_rate_last_week: float

    # Channel breakdown
    channel_conversion_rates: dict  # {"whatsapp": 0.28, "instagram": 0.09}
    channel_lead_volumes: dict      # {"whatsapp": 12, "instagram": 34}

    # Response time data
    avg_first_response_hours: float
    best_response_time_hours: float  # Their personal best

    # Timing patterns
    best_performing_days: list[str]
    best_performing_hours: list[str]

    # Benchmarks for comparison
    benchmark_conversion_rate: float | None
    benchmark_response_time_hours: float | None
    benchmark_context: str | None

    # Previous insights given (to avoid repeating)
    previous_insight_categories: list[str]

    # DNA profile for context
    dna_narrative: str | None


def build_weekly_insight_prompt(ctx: WeeklyInsightContext) -> str:
    channel_rates = "\n".join(
        f"  {ch}: {rate*100:.0f}% conversion, {ctx.channel_lead_volumes.get(ch, 0)} leads"
        for ch, rate in ctx.channel_conversion_rates.items()
    )

    benchmark_section = ""
    if ctx.benchmark_conversion_rate:
        benchmark_section = f"""
INDUSTRY BENCHMARKS (similar businesses):
- Avg conversion rate: {ctx.benchmark_conversion_rate*100:.0f}%
- Avg first response time: {ctx.benchmark_response_time_hours or 'unknown'} hours
- Additional context: {ctx.benchmark_context or 'none'}
"""

    prev_categories = ", ".join(ctx.previous_insight_categories) if ctx.previous_insight_categories else "none"

    return f"""You generate one specific, data-derived weekly insight for a business owner.

BUSINESS: {ctx.business_name} ({ctx.business_type})
WEEK: {ctx.week}

BUSINESS PERSONALITY: {ctx.dna_narrative or "Not available."}

THIS WEEK'S PERFORMANCE:
- New leads: {ctx.new_leads_this_week} (vs {ctx.new_leads_last_week} last week)
- Conversions: {ctx.conversions_this_week} (vs {ctx.conversions_last_week} last week)
- Reply rate: {ctx.reply_rate_this_week*100:.0f}% (vs {ctx.reply_rate_last_week*100:.0f}% last week)

CHANNEL PERFORMANCE:
{channel_rates or "No channel data."}

RESPONSE TIME:
- Average first response: {ctx.avg_first_response_hours:.1f} hours
- Personal best: {ctx.best_response_time_hours:.1f} hours
{benchmark_section}

TIMING PATTERNS:
- Best performing days: {', '.join(ctx.best_performing_days) or 'insufficient data'}
- Best performing times: {', '.join(ctx.best_performing_hours) or 'insufficient data'}

PREVIOUS INSIGHT CATEGORIES GIVEN (avoid repeating): {prev_categories}

---

TASK:
Generate ONE specific, actionable insight for this business owner this week.

Rules:
- This must be SPECIFIC to their data — not a generic business tip
- It must be SURPRISING — something they couldn't see themselves without the data
- It must be ACTIONABLE — one specific behaviour change they can make this week
- Pick the insight with the HIGHEST ESTIMATED IMPACT on their business
- Compare to benchmarks where available — "businesses like yours average X, you are at Y"
- Do NOT repeat a category that was used recently
- If data is limited, acknowledge it and lower confidence accordingly

Respond with ONLY valid JSON:

{{
  "category": "response_time | channel_performance | pricing | retention | conversion | pipeline | timing",
  "headline": "<punchy one-liner, max 80 chars>",
  "body": "<full insight with data, comparison, and specific recommendation — 3-5 sentences>",
  "action_item": "<the ONE thing they should do differently this week>",
  "estimated_impact": "<e.g. 'Could increase Instagram conversion by 3x based on your data'>",
  "benchmark_comparison": "<null if no benchmark available, else the comparison text>",
  "confidence": 0.0,
  "data_evidence": {{
    "key_metric": "",
    "benchmark": "",
    "gap": "",
    "opportunity": ""
  }}
}}"""
