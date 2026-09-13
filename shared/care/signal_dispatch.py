"""
The one place that decides what happens right after any Insight (a
"signal") gets created - not just stored for the /signals page, but also
pushed to a business's own outbound webhooks and, when there's a real
customer to act on, run through the Automations engine
(post_call_actions.apply_rules).

Previously this logic lived only inline in services/workers/analyse.py,
for the 4 AI-extracted kinds that happened to be wired first. Centralized
here so every Insight producer (analyse.py, recall_insights.py,
intent_leakage.py, escalation_alerts.py, health_monitor.py) calls one
function instead of duplicating the same two try/except blocks five times.

Two kinds (escalation_rate, account_health) are business-level - they
never have a customer_id, because they're about the business's own
number/escalation trend, not any one customer's conversation. apply_rules
already treats a missing customer_id as a safe no-op (nothing to send, no
customer to escalate about), so this module makes that explicit: those two
kinds still fire a webhook (a business's own external system doesn't need
a customer either), but are never sent to apply_rules and are deliberately
absent from CONDITION_FIELDS / the frontend's TRIGGER_LABEL - exposing
them as an Automations trigger would just be a second, quieter version of
the exact silent-trap bug (a selectable trigger that can never actually
fire an action) this whole pass exists to close.
"""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Insight.kind -> the WebhookEventType value it fires. Absent kind = no
# real-time dispatch at all, stays a /signals-only entry (matches today's
# behaviour for kinds intentionally left out - none are, right now; every
# kind any producer creates has an entry here).
SIGNAL_KIND_TRIGGERS: dict[str, str] = {
    # shared/ai/signals.py (AI-extracted, product_feedback-gated)
    "competitor_mention": "competitor.mentioned",
    "churn_risk": "churn_risk.detected",
    "demo_request": "demo.requested",
    "pricing_question": "pricing_question.asked",
    "bug": "bug.detected",
    "feature_request": "feature_request.detected",
    "complaint": "complaint.detected",
    "praise": "praise.detected",
    # shared/ai/recall_insights.py (deterministic, customer-scoped)
    "overdue_followup": "overdue_followup.detected",
    "report_not_collected": "report_not_collected.detected",
    "overdue_refund": "overdue_refund.detected",
    # shared/care/intent_leakage.py (deterministic, customer-scoped)
    "intent_leakage": "intent_leakage.detected",
    "rto_risk": "rto_risk.detected",
    # Business-level - webhook only, see module docstring
    "escalation_rate": "escalation_rate.detected",
    "account_health": "account_health.detected",
}


async def dispatch_signal(
    db: AsyncSession,
    *,
    business_id: uuid.UUID,
    customer_id: uuid.UUID | None,
    channel: str | None,
    kind: str,
    title: str,
    body: str,
    severity: str,
    source_quote: str | None = None,
) -> None:
    """
    Call right after adding an Insight row (before or after commit - both
    dispatch_event and apply_rules read/write within the caller's own
    session, same contract each already has independently). Never raises -
    every real failure is caught and logged, since a signal that fails to
    dispatch must not block the signal itself from being stored.
    """
    trigger_type = SIGNAL_KIND_TRIGGERS.get(kind)
    if not trigger_type:
        return

    payload: dict = {"title": title, "body": body, "severity": severity}
    if customer_id is not None:
        payload["customer_id"] = str(customer_id)
    if source_quote is not None:
        payload["source_quote"] = source_quote

    from shared.integrations import webhooks

    try:
        await webhooks.dispatch_event(db, business_id=business_id, event_type=trigger_type, payload=payload)
    except Exception:
        logger.exception("signal_dispatch webhook failed kind=%s business=%s", kind, business_id)

    if customer_id is None:
        return

    from shared.care import post_call_actions

    try:
        await post_call_actions.apply_rules(
            db, business_id=business_id, trigger_type=trigger_type, customer_id=customer_id,
            channel=channel, context={"severity": severity, "title": title, "body": body},
        )
    except Exception:
        logger.exception("signal_dispatch apply_rules failed kind=%s business=%s", kind, business_id)
