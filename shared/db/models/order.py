"""
The Order Sync capability: a local, queryable mirror of orders placed on a
business's own store platform (Shopify, WooCommerce, ...).

WISMO ("where is my order") is 30-50% of all e-commerce support volume - the
single highest-leverage thing this capability can answer honestly. The
design choice worth stating: orders are synced in via webhook and stored
here, never fetched live from the store platform per conversation turn.
Same reasoning as everywhere else in this schema (Message, Commitment,
Case, Appointment) - a local record the AI reads in milliseconds, not an
external API call in the hot path, and a real audit trail of what the
store told us and when.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class OrderStatus(str, enum.Enum):
    pending = "pending"                    # placed, not yet paid
    paid = "paid"
    fulfilled = "fulfilled"                # shipped, tracking assigned
    out_for_delivery = "out_for_delivery"
    delivered = "delivered"
    cancelled = "cancelled"
    return_requested = "return_requested"
    refunded = "refunded"


class Order(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "orders"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    # Nullable: a webhook can arrive before identity resolution finds (or
    # creates) the customer it belongs to is ever certain - never block
    # storing the order on that.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )

    # Which store platform this came from, and that platform's own id for
    # the order - the pair that makes a re-delivered webhook idempotent,
    # same pattern as Message.external_id.
    source_platform: Mapped[str] = mapped_column(String(30), nullable=False)
    external_order_id: Mapped[str] = mapped_column(String(120), nullable=False)
    order_number: Mapped[str | None] = mapped_column(String(50), nullable=True)

    status: Mapped[OrderStatus] = mapped_column(
        EnumType(OrderStatus, 20), nullable=False, default=OrderStatus.pending
    )
    items: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    total_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tracking_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    carrier: Mapped[str | None] = mapped_column(String(100), nullable=True)

    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The webhook payload as received, kept whole - an audit trail for "what
    # did the store actually tell us", the same instinct as Message.raw_payload.
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "business_id", "source_platform", "external_order_id",
            name="uq_order_per_business_platform",
        ),
        Index("idx_orders_business", "business_id"),
        Index("idx_orders_customer", "customer_id"),
    )
