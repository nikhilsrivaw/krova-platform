"""
The door a store owner actually walks through to use Order Sync: connect a
store's webhook so orders start flowing in, and see the orders that have.

Connecting is deliberately the same shape as WhatsApp - paste in credentials
generated in the platform's own dashboard, not an OAuth app install (see
routers/webhooks.py's Shopify section for why). Without this router, orders
can arrive at the webhook endpoint but no business can ever tell KROVA where
to send them or which secret to expect.
"""

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.auth.encryption import encrypt
from shared.db.models import Order, OrderStatus, StoreConnection
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/orders", tags=["orders"])


# ── Store connections ────────────────────────────────────────────────────

class StoreConnectionIn(BaseModel):
    platform: str = Field(min_length=1, max_length=30)
    store_identifier: str = Field(min_length=1, max_length=255)
    webhook_secret: str = Field(min_length=1, max_length=500)


class StoreConnectionOut(BaseModel):
    id: str
    platform: str
    store_identifier: str
    active: bool
    # webhook_secret is deliberately absent - never returned once stored,
    # same rule as ChannelConnection.access_token.


def _connection_out(c: StoreConnection) -> StoreConnectionOut:
    return StoreConnectionOut(
        id=str(c.id), platform=c.platform, store_identifier=c.store_identifier, active=c.active,
    )


@router.get("/connections", response_model=list[StoreConnectionOut])
async def list_store_connections(current_user: CurrentUserDep, db: DbDep) -> list[StoreConnectionOut]:
    rows = await db.execute(
        select(StoreConnection).where(StoreConnection.business_id == current_user.business)
    )
    return [_connection_out(c) for c in rows.scalars().all()]


@router.post("/connections", response_model=StoreConnectionOut, status_code=status.HTTP_201_CREATED)
async def connect_store(
    body: StoreConnectionIn, current_user: CurrentUserDep, db: DbDep
) -> StoreConnectionOut:
    from datetime import datetime, timezone

    connection = StoreConnection(
        business_id=current_user.business,
        platform=body.platform.lower(),
        store_identifier=body.store_identifier,
        webhook_secret=encrypt(body.webhook_secret),
        connected_at=datetime.now(timezone.utc),
    )
    db.add(connection)
    try:
        await db.flush()
    except Exception:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This store is already connected to this business.",
        )
    logger.info("store connected id=%s business=%s", connection.id, current_user.business)
    return _connection_out(connection)


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_store(connection_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
    connection = await db.get(StoreConnection, connection_id)
    if connection is None or connection.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Store connection not found")
    # Deactivate rather than delete: Order rows keep pointing at this
    # platform/store pair, and a re-connect later should not orphan history.
    connection.active = False


# ── Orders ────────────────────────────────────────────────────────────────

class OrderOut(BaseModel):
    id: str
    customer_id: str | None
    source_platform: str
    order_number: str | None
    status: str
    items: list
    total_paise: int | None
    tracking_number: str | None
    carrier: str | None
    placed_at: str


def _order_out(o: Order) -> OrderOut:
    return OrderOut(
        id=str(o.id),
        customer_id=str(o.customer_id) if o.customer_id else None,
        source_platform=o.source_platform,
        order_number=o.order_number,
        status=o.status.value if hasattr(o.status, "value") else str(o.status),
        items=o.items or [],
        total_paise=o.total_paise,
        tracking_number=o.tracking_number,
        carrier=o.carrier,
        placed_at=o.placed_at.isoformat(),
    )


@router.get("", response_model=list[OrderOut])
async def list_orders(
    current_user: CurrentUserDep,
    db: DbDep,
    customer_id: str | None = None,
    status_filter: OrderStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, le=200),
) -> list[OrderOut]:
    query = (
        select(Order)
        .where(Order.business_id == current_user.business)
        .order_by(Order.placed_at.desc())
        .limit(limit)
    )
    if customer_id:
        query = query.where(Order.customer_id == uuid.UUID(customer_id))
    if status_filter:
        query = query.where(Order.status == status_filter)
    rows = await db.execute(query)
    return [_order_out(o) for o in rows.scalars().all()]


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> OrderOut:
    order = await db.get(Order, order_id)
    if order is None or order.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return _order_out(order)


class OrderPatch(BaseModel):
    status: OrderStatus | None = None
    tracking_number: str | None = None
    carrier: str | None = None


@router.patch("/{order_id}", response_model=OrderOut)
async def update_order(order_id: uuid.UUID, body: OrderPatch, current_user: CurrentUserDep, db: DbDep) -> OrderOut:
    """
    A human moving an order forward - the only way a WhatsApp-native order
    (source_platform="whatsapp") ever changes status at all, since Meta
    sends no separate payment or fulfillment webhook for one the way
    Shopify does. Also usable on a synced Shopify order for a manual
    correction, though the webhook is that one's real source of truth.
    """
    order = await db.get(Order, order_id)
    if order is None or order.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    if body.status is not None:
        order.status = body.status
    if body.tracking_number is not None:
        order.tracking_number = body.tracking_number
    if body.carrier is not None:
        order.carrier = body.carrier
    await db.flush()
    return _order_out(order)
