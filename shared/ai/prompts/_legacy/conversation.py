"""
KROVA — Mobile App Conversation Prompt
Powers the real-time chat between the business owner and KROVA.
Claude Sonnet reads business context + last night's analysis + conversation history
and answers the owner's question in plain language.

The owner experience: "Aaj kya hua?" → KROVA answers everything about their business.
"""

from __future__ import annotations


def build_conversation_prompt(
    business_name: str,
    business_type: str,
    business_context: str | None,
    analysis_summary: str | None,
    hot_leads: list[dict],
    at_risk_customers: list[dict],
    pending_actions_count: int,
    conversation_history: list[dict],  # [{role: user|assistant, content: str}]
    owner_question: str,
) -> list[dict]:
    """
    Build the full message list for a Claude Sonnet streaming call.
    Returns a list of messages in Anthropic API format.

    Args:
        conversation_history: Previous messages in this session (last 20)
        owner_question: The owner's current question
    """
    system_prompt = _build_system_prompt(
        business_name=business_name,
        business_type=business_type,
        business_context=business_context,
        analysis_summary=analysis_summary,
        hot_leads=hot_leads,
        at_risk_customers=at_risk_customers,
        pending_actions_count=pending_actions_count,
    )

    # Build message history — prepend system context to first user message
    messages: list[dict] = []
    for msg in conversation_history:
        messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": owner_question})

    return [{"system": system_prompt, "messages": messages}]


def _build_system_prompt(
    business_name: str,
    business_type: str,
    business_context: str | None,
    analysis_summary: str | None,
    hot_leads: list[dict],
    at_risk_customers: list[dict],
    pending_actions_count: int,
) -> str:
    hot_leads_text = ""
    if hot_leads:
        lines = [
            f"  - {l.get('name') or l.get('phone') or 'Unknown'}: {l.get('reasoning', '')}"
            for l in hot_leads[:5]
        ]
        hot_leads_text = "HOT LEADS RIGHT NOW:\n" + "\n".join(lines)
    else:
        hot_leads_text = "HOT LEADS RIGHT NOW: None currently"

    at_risk_text = ""
    if at_risk_customers:
        lines = [
            f"  - {c.get('name') or c.get('phone') or 'Unknown'}: {c.get('reasoning', '')}"
            for c in at_risk_customers[:5]
        ]
        at_risk_text = "AT-RISK CUSTOMERS:\n" + "\n".join(lines)
    else:
        at_risk_text = "AT-RISK CUSTOMERS: None currently"

    pending_text = f"PENDING APPROVALS: {pending_actions_count} follow-ups waiting for your approval"

    return f"""You are KROVA — the AI business partner for {business_name}.

You know this business completely. You have been watching it every day.
You are NOT a generic AI assistant — you are the dedicated intelligence for THIS specific business.

ABOUT THIS BUSINESS:
Type: {business_type}
Context: {business_context or "Not set up yet."}

LAST NIGHT'S ANALYSIS:
{analysis_summary or "No analysis run yet. Data collection just started."}

{hot_leads_text}

{at_risk_text}

{pending_text}

YOUR JOB:
- Answer the owner's questions about their business with specific names, numbers, and recommendations
- Be direct and practical — the owner is busy
- Speak in the same language the owner uses (Hindi or English or Hinglish)
- When suggesting a follow-up message, write the exact message they should send
- Reference specific customers by name when you know them
- If you don't have data yet, say so honestly — don't make things up
- Keep responses concise — the owner reads this on their phone

You are their trusted business partner who never sleeps and always has the latest picture of their business."""
