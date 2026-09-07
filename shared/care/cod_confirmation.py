"""
Handling a COD confirm/decline button tap - deterministic, never
agent-mediated. A tap on shared.scheduling.notify.send_cod_confirmation's
own template is exactly the kind of no-judgment-needed event that should
never cost an LLM call, mirroring services/workers/respond.py's own
existing channel == "voice" early-return for the identical reason.

Matched on shared/channels/whatsapp/webhook.py's own `button.payload`
field (added for this), never on the button's visible `text` - a display
label is not a stable identifier a second template could not also use.

Order Sync is receive-only (see StoreConnection's own docstring) - a
decline here can never cancel the real Shopify order, only notify staff
to do it themselves. This module never claims otherwise.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Business, Customer, Message, Order, OrderStatus
from shared.utils.logging import get_logger

logger = get_logger(__name__)

CONFIRM_PAYLOAD = "COD_CONFIRM"
DECLINE_PAYLOAD = "COD_DECLINE"


def button_payload(message: Message) -> str | None:
    """None for every message that isn't a COD button tap - the caller's
    own signal to fall through to the normal reply pipeline unchanged."""
    payload = (message.media or {}).get("payload")
    return payload if payload in (CONFIRM_PAYLOAD, DECLINE_PAYLOAD) else None


async def _resolve_single_pending(customer_id, db: AsyncSession) -> list[Order]:
    return list(
        (
            await db.execute(
                select(Order).where(
                    Order.customer_id == customer_id,
                    Order.is_cod.is_(True),
                    Order.status == OrderStatus.pending,
                    Order.cod_confirmed_at.is_(None),
                    Order.cod_declined_at.is_(None),
                )
            )
        ).scalars().all()
    )


async def resolve_and_apply(
    *, customer: Customer, business: Business, confirmed: bool, channel: str, db: AsyncSession
) -> Order | None:
    """
    The shared core behind both entry points - a WhatsApp button tap
    (handle(), below) and the COD voice-call failsafe's DTMF digits
    (shared/channels/voice/cod_ivr.py). Same disambiguation rule either
    way: a customer's single most recent pending, unanswered COD order is
    unambiguous in the common case; anything else escalates rather than
    guessing which order the customer meant. Returns the order acted on,
    or None if it escalated instead (nothing to act on).
    """
    from shared.ai import agent as agent_module

    candidates = await _resolve_single_pending(customer.id, db)

    if len(candidates) != 1:
        logger.info(
            "COD confirmation ambiguous business=%s customer=%s candidates=%d channel=%s",
            business.id, customer.id, len(candidates), channel,
        )
        await agent_module.notify_escalation(
            business.id,
            reason=(
                "Customer tried to confirm a COD order but no single pending "
                "COD order could be matched - needs manual follow-up"
                if not candidates else
                "Customer tried to confirm a COD order with multiple pending "
                "COD orders open - needs manual follow-up to confirm which one"
            ),
            customer_id=customer.id, channel=channel, db=db,
        )
        return None

    order = candidates[0]
    now = datetime.now(timezone.utc)

    if confirmed:
        order.cod_confirmed_at = now
        logger.info("COD order confirmed id=%s business=%s channel=%s", order.id, business.id, channel)
        return order

    order.cod_declined_at = now
    logger.info("COD order declined id=%s business=%s channel=%s", order.id, business.id, channel)
    await agent_module.notify_escalation(
        business.id,
        reason=f"Customer declined COD order #{order.order_number or order.id} - cancel it in Shopify",
        customer_id=customer.id, channel=channel, db=db,
    )
    return order


async def handle(message: Message, payload: str, db: AsyncSession) -> None:
    """
    Resolve which order a WhatsApp button tap answers, and act on it. Thin
    wrapper over resolve_and_apply - kept as its own function since
    draft_for_message's own early-return already has a Message in hand,
    not a bare customer/business pair.
    """
    customer = await db.get(Customer, message.customer_id)
    business = await db.get(Business, message.business_id)
    if customer is None or business is None:
        return
    await resolve_and_apply(
        customer=customer, business=business,
        confirmed=(payload == CONFIRM_PAYLOAD), channel="whatsapp", db=db,
    )
