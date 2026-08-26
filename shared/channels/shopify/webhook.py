"""
Parsing Shopify order webhooks.

Shopify sends one topic per webhook (orders/create, orders/updated,
orders/fulfilled, orders/cancelled, orders/paid, ...) in an
X-Shopify-Topic header, each carrying the full current Order object - not a
diff. That shape is what makes this simple: every webhook is handled the
same way, an upsert keyed on (business, platform, external_order_id), and
the receiver never needs to branch on which topic arrived.

Money is a string like "49.99" in Shopify's payload; converted to integer
paise immediately; nothing downstream ever touches a float.

Status is deliberately conservative. Shopify's own fields (financial_status,
fulfillment_status, cancelled_at) tell us pending / paid / fulfilled /
cancelled / refunded honestly. "out_for_delivery" and "delivered" are
courier facts Shopify does not carry in this payload - inventing them from
"it was fulfilled N days ago" would be exactly the kind of guess the
vertical's own policy ("never state an order status you have not been
given") forbids. They stay unset until a real courier-tracking source
exists to report them.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from shared.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class ParsedOrder:
    external_order_id: str
    order_number: str | None
    status: str  # OrderStatus value - kept as str here, mapped by the caller
    items: list[dict]
    total_paise: int | None
    tracking_number: str | None
    carrier: str | None
    placed_at: datetime
    customer_email: str | None
    customer_phone: str | None
    raw: dict[str, Any] = field(default_factory=dict)


def _to_paise(value: Any) -> int | None:
    """Shopify sends price as a decimal string, e.g. "49.99". Never a float."""
    if value is None:
        return None
    try:
        from decimal import Decimal

        return int((Decimal(str(value)) * 100).to_integral_value())
    except Exception:
        logger.warning("unreadable order total %r", value)
        return None


def _timestamp(value: Any) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        # Shopify sends ISO 8601 with an explicit offset, e.g.
        # "2026-08-26T10:00:00-04:00" - fromisoformat handles that directly.
        return datetime.fromisoformat(str(value))
    except ValueError:
        logger.warning("unreadable order timestamp %r, using now", value)
        return datetime.now(timezone.utc)


def _status(payload: dict) -> str:
    """See the module docstring: honest mapping only, never a courier guess."""
    if payload.get("cancelled_at"):
        return "cancelled"

    financial = payload.get("financial_status")
    if financial in ("refunded", "partially_refunded"):
        return "refunded"

    if payload.get("fulfillment_status") == "fulfilled":
        return "fulfilled"

    if financial == "paid":
        return "paid"

    return "pending"


def _items(payload: dict) -> list[dict]:
    out = []
    for item in payload.get("line_items") or []:
        out.append(
            {
                "title": item.get("title") or item.get("name"),
                "quantity": item.get("quantity"),
                "price_paise": _to_paise(item.get("price")),
            }
        )
    return out


def _tracking(payload: dict) -> tuple[str | None, str | None]:
    """First fulfillment with a tracking number - a split shipment has more
    than one, but the common case (one parcel) is what a WISMO reply needs."""
    for fulfillment in payload.get("fulfillments") or []:
        number = fulfillment.get("tracking_number")
        if number:
            return number, fulfillment.get("tracking_company")
    return None, None


def parse_order(payload: dict) -> ParsedOrder | None:
    """
    Turn one Shopify order webhook body into a normalised order.

    Never raises on a malformed payload - same reasoning as the WhatsApp
    parser: Shopify expects a 200 quickly, and an unhandled exception here
    should not cost every future webhook a retry storm. Returns None (not a
    partial record) when the payload is missing the one field nothing can
    be inferred without - the order's own id.
    """
    external_id = payload.get("id")
    if external_id is None:
        logger.warning("shopify order webhook missing id - dropped")
        return None

    tracking_number, carrier = _tracking(payload)
    customer = payload.get("customer") or {}
    shipping = payload.get("shipping_address") or {}

    return ParsedOrder(
        external_order_id=str(external_id),
        order_number=(
            str(payload["order_number"]) if payload.get("order_number") is not None else None
        ),
        status=_status(payload),
        items=_items(payload),
        total_paise=_to_paise(payload.get("total_price")),
        tracking_number=tracking_number,
        carrier=carrier,
        placed_at=_timestamp(payload.get("created_at")),
        customer_email=payload.get("email") or customer.get("email"),
        customer_phone=payload.get("phone") or customer.get("phone") or shipping.get("phone"),
        raw=payload,
    )
