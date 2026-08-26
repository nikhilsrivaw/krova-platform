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

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class StoreConnection(UUIDMixin, TimestampMixin, Base):
    """
    One connected store platform - Shopify, WooCommerce, ... - on one business.

    A row, not a bag of keys on the business, for the same reason
    ChannelConnection is: a business with two stores (a main site and a
    seasonal one) needs two webhook secrets, not one column overwritten by
    whichever was configured last.

    webhook_secret is what verifies an inbound webhook actually came from
    the platform, not what authenticates outbound calls to it - Order Sync
    only ever receives, per its own design note, so there is no API token
    to store here yet.
    """

    __tablename__ = "store_connections"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )

    # "shopify" | "woocommerce" - lowercase, matches Order.source_platform.
    platform: Mapped[str] = mapped_column(String(30), nullable=False)
    # The store's own identifier for itself - a Shopify *.myshopify.com
    # domain, a WooCommerce site URL. Shown to the owner so a confused
    # dashboard reads "yourstore.myshopify.com is connected", not a UUID.
    store_identifier: Mapped[str] = mapped_column(String(255), nullable=False)

    # Encrypted at rest, same as ChannelConnection.access_token. Never
    # logged, never returned by the API.
    webhook_secret: Mapped[str] = mapped_column(String(500), nullable=False)

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "business_id", "platform", "store_identifier",
            name="uq_store_connection_per_business",
        ),
        Index("idx_store_connections_business", "business_id"),
        # The webhook receiver's actual query: which business owns this
        # store, given only what Shopify's request tells us (platform +
        # its own shop domain) - business_id is not known yet at that point,
        # so the lookup cannot lead with it the way the unique constraint's
        # index does.
        Index("idx_store_connections_lookup", "platform", "store_identifier"),
    )


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
