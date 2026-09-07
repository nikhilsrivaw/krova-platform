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

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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

    # Extracted once at parse time from the same payload_gateway_names
    # field the COD-confirmation sweep needs to query on repeatedly -
    # same reasoning status/items/tracking are already extracted onto
    # real columns rather than re-parsed from raw_payload on every read.
    is_cod: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The webhook payload as received, kept whole - an audit trail for "what
    # did the store actually tell us", the same instinct as Message.raw_payload.
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Set by the deterministic COD confirm/decline button-tap handler in
    # services/workers/respond.py - never by the agent, never inferred.
    # Local-only: Order Sync is receive-only (see StoreConnection's own
    # docstring), so a decline notifies staff to cancel it in Shopify
    # themselves rather than KROVA claiming to cancel the real order.
    cod_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cod_declined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Dedupe for the proactive confirmation send itself - same
    # "add a tracking column so a scheduler sweep never resends" shape as
    # review_requested_at. Separate from cod_confirmed_at/declined_at:
    # this tracks whether we asked, those track how the customer answered.
    cod_confirmation_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Set by the Shiprocket NDR poll (shared/care/shiprocket_sync.py) -
    # the one real courier fact this schema was always missing, per this
    # module's own docstring on why out_for_delivery/delivered stay unset
    # from Shopify's payload alone.
    ndr_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ndr_reschedule_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Same dedupe shape as review_requested_at, for a different sweep
    # (shared/scheduling/recall.py's send_repeat_purchase_nudges) -
    # deliberately its own column, not a reuse of review_requested_at,
    # which marks a different event (a review was asked for, not a
    # repeat purchase).
    repeat_purchase_nudge_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Stamped by the COD voice-call failsafe (shared/care/cod_call_failsafe.py)
    # once a call has actually been placed - separate from
    # cod_confirmation_sent_at (the WhatsApp template) the same way that
    # column is separate from cod_confirmed_at/cod_declined_at: this tracks
    # whether we called, not how the customer answered.
    cod_call_placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A permanent "already escalated for no outcome" marker - separate from
    # cod_call_placed_at so escalate_no_outcome fires exactly once per
    # order, the same one-shot shape as Escalation.escalated_further_at
    # (shared/care/escalation_failsafe.py), not a re-fire every sweep.
    cod_call_escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Shopify's shipping_address.zip, captured alongside the phone number
    # already pulled from that same dict in shared/channels/shopify/
    # webhook.py::parse_order - what shared/care/intent_leakage.py's
    # check_rto_risk_pincodes sweep groups a business's own NDR history by.
    shipping_pincode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Dedupe for the RTO-risk sweep - same "stamp so a sweep never
    # reprocesses" shape as every other *_sent_at column here, just not a
    # send this time.
    rto_risk_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "business_id", "source_platform", "external_order_id",
            name="uq_order_per_business_platform",
        ),
        Index("idx_orders_business", "business_id"),
        Index("idx_orders_customer", "customer_id"),
        # The RTO-risk sweep's own query: this business's prior NDR orders
        # sharing a pincode with a new COD order.
        Index("idx_orders_pincode_ndr", "business_id", "shipping_pincode", "ndr_at"),
    )


class AbandonedCheckout(UUIDMixin, TimestampMixin, Base):
    """
    A Shopify checkout that never became an Order - a genuinely different
    real-world object, not a status Order itself can express: it may
    never convert, and cramming "might not exist yet" into a table whose
    whole point is "a real, placed order" would misrepresent what a
    completed order actually is. Same idempotent-upsert-by-external-id
    shape as Order, deliberately - the webhook receiver for this table
    mirrors _process_shopify_order almost exactly.
    """

    __tablename__ = "abandoned_checkouts"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )

    source_platform: Mapped[str] = mapped_column(String(30), nullable=False)
    external_checkout_id: Mapped[str] = mapped_column(String(120), nullable=False)

    items: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    total_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Shopify's own resume-checkout link - what the recovery message
    # actually sends, not a link KROVA constructs itself.
    checkout_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    customer_phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    customer_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    abandoned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recovery_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Stamped if a real Order for this customer appears after the
    # checkout was abandoned - the recovery sweep's own dedupe signal,
    # and what the intent-leakage sweep excludes on (a recovered
    # checkout is not leaked intent, it converted).
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "business_id", "source_platform", "external_checkout_id",
            name="uq_checkout_per_business_platform",
        ),
        Index("idx_abandoned_checkouts_business", "business_id"),
        Index("idx_abandoned_checkouts_customer", "customer_id"),
        # The recovery sweep's own query: unrecovered, not yet nudged,
        # within the recovery window.
        Index("idx_abandoned_checkouts_sweep", "business_id", "recovery_sent_at", "recovered_at"),
    )


class ShippingConnection(UUIDMixin, TimestampMixin, Base):
    """
    A business's own Shiprocket account - a shipping platform, not a
    store platform. Deliberately not folded into StoreConnection:
    StoreConnection.platform already means "where orders come from"
    (Shopify/WooCommerce); a business's orders still come from Shopify
    either way, Shiprocket is who reports what happened to the parcel
    after. Different relationship, same "a row, not a bag of keys"
    reasoning as every other *Connection table in this schema.

    Shiprocket's own auth is login-based (email+password exchanged for a
    bearer token, confirmed against apiv2.shiprocket.in/v1/external/
    auth/login), not a static API key like ChannelConnection's Meta
    tokens - both credentials are stored, encrypted, because the token
    itself expires every 240 hours and needs re-login, not a refresh
    grant.
    """

    __tablename__ = "shipping_connections"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    # "shiprocket" - free text like StoreConnection.platform, same reason.
    platform: Mapped[str] = mapped_column(String(30), nullable=False)

    # Both encrypted at rest. email+password re-authenticate when the
    # token expires; the token itself is cached here so most calls don't
    # need a fresh login.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password: Mapped[str] = mapped_column(Text, nullable=False)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("business_id", "platform", name="uq_shipping_connection_per_business"),
        Index("idx_shipping_connections_business", "business_id"),
    )
