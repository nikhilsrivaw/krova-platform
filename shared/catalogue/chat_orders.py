"""
An order taken in a chat. Staff tick products from the business's own list (the Catalogue page - Shopify, the Meta
catalogue, a file, or typed in), set how many, and either send a payment link for the total or take it as cash on
delivery. The order lands in the same Order table as Shopify and WhatsApp-cart orders (source_platform "chat"), so
the Orders page, the AI's "where is my order" answers and every order automation pick it up unchanged.

Prices come from the product list on the server, never from the screen, and each order keeps its own copy of the
lines, so a later price change leaves it alone.

Payment: the link is an ordinary payment link on a Commitment (source_system "order_payment", external_ref the
order id) - the same link, 2-minute paid check and Ledger entry a token's fee uses (shared/payments/links.py). When
that commitment is fully paid - by the link or by a receipt recorded on the Ledger - the order becomes "paid", its
order automations run, and Meta hears of the purchase if the customer came from an ad. The same link works for an
order that arrived as a WhatsApp cart.

COD: the customer agreed in the chat, so the order is marked confirmed at once - KROVA's COD confirmation message
and call (for orders from a website) do not go out for it.
"""

import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    Business, Channel, ChannelConnection, Commitment, CommitmentStatus, ConnectionStatus, Customer, Order, OrderStatus,
    Product, ProductVariant,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

SOURCE = "chat"
# Orders a link may be sent for: taken on WhatsApp. A Shopify or WooCommerce order is paid through its own store.
LINKABLE_SOURCES = (SOURCE, "whatsapp")
ORDER_PAYMENT = "order_payment"
MAX_LINES = 30
MAX_QUANTITY = 99
# Shopify's name for a product with no options - not worth repeating after the product's own name.
_PLAIN_VARIANT = {"default title", "default"}


class OrderError(ValueError):
    """Something staff should see, e.g. a product without a price."""


def line_name(product_title: str, variant_title: str | None) -> str:
    if variant_title and variant_title.strip().lower() not in _PLAIN_VARIANT and variant_title.strip() != product_title:
        return f"{product_title} - {variant_title.strip()}"[:200]
    return product_title[:200]


async def build_items(db: AsyncSession, business_id: uuid.UUID, lines: list[tuple[uuid.UUID, int]]) -> list[dict[str, Any]]:
    """The order's lines, priced from the business's own product list. Raises OrderError for anything not sellable."""
    if not lines:
        raise OrderError("Pick at least one product.")
    if len(lines) > MAX_LINES:
        raise OrderError(f"An order can have at most {MAX_LINES} products.")
    ids = [variant_id for variant_id, _ in lines]
    rows = (
        await db.execute(
            select(ProductVariant, Product.title)
            .join(Product, Product.id == ProductVariant.product_id)
            .where(ProductVariant.id.in_(ids), ProductVariant.business_id == business_id)
        )
    ).all()
    found = {variant.id: (variant, title) for variant, title in rows}
    items: list[dict[str, Any]] = []
    for variant_id, quantity in lines:
        if variant_id not in found:
            raise OrderError("One of the products is no longer on your product list. Refresh and pick again.")
        if not 1 <= quantity <= MAX_QUANTITY:
            raise OrderError(f"Quantity must be between 1 and {MAX_QUANTITY}.")
        variant, title = found[variant_id]
        name = line_name(title, variant.title)
        if not variant.price_paise:
            raise OrderError(f"{name} has no price on the Catalogue page. Add one first.")
        if variant.available is False:
            raise OrderError(f"{name} is marked unavailable.")
        items.append({
            "variant_id": str(variant.id), "product_id": str(variant.product_id), "title": name, "sku": variant.sku,
            "quantity": quantity, "price_paise": variant.price_paise,
        })
    return items


def total(items: list[dict[str, Any]]) -> int:
    return sum(int(i.get("price_paise") or 0) * int(i.get("quantity") or 1) for i in items or [])


def label(items: list[dict[str, Any]]) -> str:
    """"2 × Kurta - M, Dupatta" - for the payment link and the Ledger."""
    if any(not i.get("title") for i in items or []):
        # a WhatsApp cart carries only Meta's product ids - a count reads better than those
        count = sum(int(i.get("quantity") or 1) for i in items)
        return f"{count} item{'' if count == 1 else 's'}"
    parts = []
    for i in items or []:
        name = i["title"]
        qty = int(i.get("quantity") or 1)
        parts.append(f"{qty} × {name}" if qty > 1 else name)
    return ", ".join(parts)[:200]


async def create(
    db: AsyncSession, *, business: Business, customer: Customer, items: list[dict[str, Any]], cod: bool,
    note: str | None, user_id: uuid.UUID | None,
) -> Order:
    now = datetime.now(timezone.utc)
    order = Order(
        business_id=business.id, customer_id=customer.id, source_platform=SOURCE,
        external_order_id=uuid.uuid4().hex, order_number=f"WA-{secrets.token_hex(3).upper()}",
        status=OrderStatus.pending, items=items, total_paise=total(items), is_cod=cod, placed_at=now,
        raw_payload={"taken_in_chat_by": str(user_id) if user_id else None, "note": (note or "").strip()[:500] or None},
        cod_confirmed_at=now if cod else None,
    )
    db.add(order)
    await db.flush()
    logger.info("chat order business=%s order=%s lines=%d cod=%s", business.id, order.id, len(items), cod)
    return order


async def send_link(
    db: AsyncSession, *, business: Business, order: Order, user_id: uuid.UUID | None,
    template_name: str | None = None, template_language: str = "en",
):
    """
    A payment link for what is still owed on an order - a chat order or a WhatsApp cart. Once per order: one already
    out and still open is returned, not sent again. Returns (link, sent, reason) like links.send_fee_link.
    """
    from shared.payments import links

    if order.business_id != business.id:
        raise links.LinkError("Order not found.")
    if order.source_platform not in LINKABLE_SOURCES:
        raise links.LinkError("This order is paid through its own store, not by a KROVA link.")
    if order.customer_id is None:
        raise links.LinkError("This order has no customer to send a link to.")
    if order.status != OrderStatus.pending:
        raise links.LinkError("A payment link is only for an order that is still unpaid.")
    if not order.total_paise:
        raise links.LinkError("This order has no total to collect.")
    what = label(order.items) or "your order"
    commitment = await links._fee_commitment_for(
        db, business_id=business.id, customer_id=order.customer_id, amount_paise=order.total_paise,
        description=f"Order {order.order_number}: {what}" if order.order_number else f"WhatsApp order: {what}",
        source_system=ORDER_PAYMENT, external_ref=str(order.id),
    )
    if commitment.status != CommitmentStatus.open or not commitment.outstanding_paise:
        return None, False, "This order is already paid."
    existing = await links._open_link_for(db, commitment.id, datetime.now(timezone.utc))
    if existing is not None:
        return existing, False, "A link for this order was already sent. Copy it to share it again."
    return await links.send_link(
        db, business=business, commitment=commitment, user_id=user_id,
        template_name=template_name, template_language=template_language,
        summary=await summary_text(db, order) if order.source_platform == SOURCE else None,
    )


async def summary_text(db: AsyncSession, order: Order) -> str:
    """
    What the customer is told about an order taken in a chat: each line with its price, the total, how they pay, and
    the business's own return and refund policy (Business Card -> "Cancel, return and refund") - the price breakdown
    and policy the Consumer Protection (E-Commerce) Rules, 2020 ask a seller to give.
    """
    from shared.db.models import KnowledgeItem
    from shared.payments.links import rupees

    lines = [f"Order {order.order_number}" if order.order_number else "Your order"]
    for item in order.items or []:
        qty = int(item.get("quantity") or 1)
        price = int(item.get("price_paise") or 0)
        lines.append(f"{qty} × {item.get('title') or 'item'} - {rupees(price * qty)}")
    lines.append(f"Total: {rupees(order.total_paise or 0)}")
    if order.is_cod:
        lines.append("Payment: cash on delivery / delivery par cash")
    policy = (
        await db.execute(
            select(KnowledgeItem.content).where(
                KnowledgeItem.business_id == order.business_id, KnowledgeItem.topic == "changes",
                KnowledgeItem.active.is_(True),
            )
        )
    ).scalars().first()
    if policy and policy.strip():
        lines.append(f"Returns and refunds: {policy.strip()[:300]}")
    return "\n".join(lines)


async def send_summary(db: AsyncSession, *, business: Business, order: Order, user_id: uuid.UUID | None) -> tuple[bool, str | None]:
    """
    A cash-on-delivery order's summary, on WhatsApp - only inside the 24-hour window (a plain message; a template
    would need its own approval). Returns (sent, reason). Never raises: the order stands either way.
    """
    from shared.channels import ingest
    from shared.channels.whatsapp.client import WhatsAppClient, WhatsAppError, within_service_window
    from shared.auth.encryption import decrypt
    from shared.db.models import Channel, Direction, IdentityKind
    from shared.payments import links

    try:
        customer = await db.get(Customer, order.customer_id) if order.customer_id else None
        if customer is None:
            return False, "No customer on this order."
        if getattr(customer, "is_private", False):
            return False, "This is a private conversation."
        phone = await links._phone(db, customer.id)
        if not phone:
            return False, "This customer has no phone number on file."
        if not within_service_window(await links._last_inbound(db, customer.id)):
            return False, "The customer hasn't written in 24 hours, so the summary wasn't sent."
        wa = await links._whatsapp(db, business.id)
        text = await summary_text(db, order)
        result = await WhatsAppClient(decrypt(wa.access_token), wa.external_account_id).send_text(phone, text)
        await ingest.ingest(
            business_id=business.id, channel=Channel.whatsapp, direction=Direction.outbound,
            identity_kind=IdentityKind.phone, identity_value=phone, external_id=result.external_id,
            text=text, occurred_at=datetime.now(timezone.utc), connection_id=wa.id, enqueue_analysis=False,
            sent_by_user_id=user_id, db=db,
        )
        return True, None
    except (links.LinkError, WhatsAppError) as exc:
        return False, str(exc)[:200]
    except Exception:  # noqa: BLE001
        logger.exception("order summary not sent order=%s", order.id)
        return False, "The summary could not be sent."


async def settle_if_paid(db: AsyncSession, commitment: Commitment) -> Order | None:
    """
    Called whenever money lands on a commitment. When it is an order's payment and it is now fully paid, the order
    becomes "paid": order automations run and Meta hears of the purchase. Never raises.
    """
    if commitment.source_system != ORDER_PAYMENT or commitment.status != CommitmentStatus.met:
        return None
    try:
        order = await db.get(Order, uuid.UUID(commitment.external_ref))
    except (TypeError, ValueError):
        return None
    if order is None or order.business_id != commitment.business_id or order.status != OrderStatus.pending:
        return None
    order.status = OrderStatus.paid
    await db.flush()
    try:
        from shared.care import post_call_actions

        if order.customer_id:
            await post_call_actions.fire_safely(
                db, business_id=order.business_id, trigger_type="order.status_changed", customer_id=order.customer_id,
                context={
                    "status": "paid", "previous_status": "pending", "order_number": order.order_number,
                    "total_paise": order.total_paise, "is_cod": order.is_cod,
                },
            )
        await _purchase_conversion(db, order)
    except Exception:  # noqa: BLE001 - the payment is recorded whatever happens here
        logger.exception("order paid follow-ups failed order=%s", order.id)
    return order


async def _purchase_conversion(db: AsyncSession, order: Order) -> None:
    """Tell Meta a customer who came from an ad bought - the same event a paid Shopify order sends."""
    from shared.auth.encryption import decrypt
    from shared.channels.whatsapp import conversions

    customer = await db.get(Customer, order.customer_id) if order.customer_id else None
    if customer is None or not customer.ctwa_clid:
        return
    connection = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == order.business_id,
                ChannelConnection.channel == Channel.whatsapp,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()
    if connection is None or not connection.access_token:
        return
    try:
        await conversions.send_conversion_for_customer(
            dataset_id=(connection.extra or {}).get("dataset_id"), access_token=decrypt(connection.access_token),
            customer_ctwa_clid=customer.ctwa_clid, event_name="Purchase", value_paise=order.total_paise,
        )
    except conversions.ConversionEventError:
        logger.warning("purchase conversion failed order=%s", order.id)
