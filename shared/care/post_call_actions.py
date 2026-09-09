"""
The voice-to-action bridge - interprets PostCallActionRule rows.

No rules engine, or anything like it, exists anywhere else in this
codebase - every other conditional action here is a hardcoded Python
function wired as one fixed scheduler job (cod_call_failsafe.py's
WhatsApp-unanswered -> call -> escalate chain is the closest analog).
This is genuinely new infrastructure: a business picks a trigger and an
action themselves, rather than a developer hardcoding the pairing.

Called from the two places a call's outcome actually exists (confirmed,
not unified into one taxonomy this round - see WebhookEventType's own
comment): shared/channels/voice/outbound.py's outbound_hangup
(voicemail/no_answer - no Call row exists for these at all) and
shared/channels/voice/relay.py's _analyze_call (completed - a real Call
row with an outcome).

v1 ships exactly two action types with real, working implementations -
deliberately not an open-ended action language (arbitrary webhooks,
arbitrary email templates). Adding a third is a new `elif`, not a schema
change - action_config is already free-form JSON per rule.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Customer, PostCallActionRule
from shared.utils.logging import get_logger

logger = get_logger(__name__)


async def apply_rules(
    db: AsyncSession, *, business_id: uuid.UUID, trigger_type: str, customer_id: uuid.UUID | None,
) -> int:
    """Run every active rule matching this trigger for this business. Returns how many actions ran."""
    if customer_id is None:
        # Every action type today needs a customer (a WhatsApp send, an
        # Escalation row tied to who the call was about) - nothing to do
        # for a call with no resolved customer at all.
        return 0

    result = await db.execute(
        select(PostCallActionRule).where(
            PostCallActionRule.business_id == business_id,
            PostCallActionRule.trigger_type == trigger_type,
            PostCallActionRule.is_active.is_(True),
        )
    )
    rules = result.scalars().all()
    if not rules:
        return 0

    customer = await db.get(Customer, customer_id)
    if customer is None:
        return 0

    from shared.db.models import Business

    business = await db.get(Business, business_id)
    if business is None:
        return 0

    ran = 0
    for rule in rules:
        try:
            if rule.action_type == "whatsapp_followup":
                from shared.scheduling import notify

                message = (rule.action_config or {}).get("message") or ""
                if not message:
                    logger.warning("post_call_action_rule=%s has no message configured, skipping", rule.id)
                    continue
                if await notify.send_post_call_followup(db, business=business, customer=customer, message=message):
                    ran += 1

            elif rule.action_type == "create_escalation_task":
                from shared.ai import agent as agent_module

                reason = (rule.action_config or {}).get("reason") or f"Post-call follow-up needed ({trigger_type})"
                await agent_module.notify_escalation(
                    business_id, reason=reason, customer_id=customer_id, channel="voice", db=db,
                )
                ran += 1

            else:
                logger.warning("post_call_action_rule=%s has unrecognised action_type=%s", rule.id, rule.action_type)
        except Exception:
            logger.exception("post_call_action_rule=%s failed to apply", rule.id)

    return ran
