"""
The product catalogue: what a business actually sells.

Until now every product fact in this schema lived as untyped JSONB inside
Order.items - enough to answer "what was in this order," and nothing else.
That one gap blocked five separate things, each of which kept arriving at
the same wall:

    a Demand Board that can say "47 people asked for Blue" but not
      "...and you don't stock Blue"
    per-SKU RTO (some products come back far more than others)
    replenishment timing, which needs a per-product consumption cycle
    authenticity/batch-code verification
    bundle and cross-sell suggestions

Modelled as Product -> ProductVariant because that is the shape the source
data actually has, and because the variant is where the interesting
question lives: a customer asking for "the blue one in XL" is asking about
a variant, not a product. Flattening the two would make the single most
valuable query in the Demand Board impossible to express.

**How rows get here, and the constraint that decided it**: StoreConnection
stores a webhook_secret and no access token, so this platform cannot call
Shopify's API to pull a catalogue - it can only receive what Shopify
pushes. So population is webhook-first (products/create, products/update,
same HMAC path orders already use), with a file import as the
source-neutral fallback for businesses on WooCommerce, another platform,
or no platform at all. Nothing here assumes Shopify.

Inventory is deliberately nullable throughout. A business that never tells
us its stock levels still gets a useful catalogue - "you don't sell this
at all" is a different and more reliable answer than "you're out of
stock," and only the first one needs inventory to be absent rather than
wrong.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin


class Product(UUIDMixin, TimestampMixin, Base):
    """
    One sellable thing, as the business's own store describes it.

    Keyed by (business_id, source_platform, external_id) for the same
    reason Order is - a webhook Shopify retries three times must produce
    one row, not three. source_platform is part of the key so a business
    migrating from WooCommerce to Shopify does not collide its old
    catalogue with its new one.
    """

    __tablename__ = "products"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )

    # "shopify" | "woocommerce" | "manual" - lowercase, matching
    # Order.source_platform's own convention. "manual" covers both the
    # file import and anything typed in by hand.
    source_platform: Mapped[str] = mapped_column(String(30), nullable=False)

    # The platform's own id. Null only for manually created products,
    # which have no upstream identity to point back at.
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False)

    # Shopify's product_type / vendor, or whatever the source calls them.
    # Kept because they are the cheapest useful way to group a catalogue
    # ("all your skincare RTOs at 31%") without anyone tagging anything.
    product_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(255), nullable=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # active | archived | draft. A product a business stopped selling is
    # not deleted - "you used to sell this and stopped" is a real answer
    # to a customer asking for it.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "business_id", "source_platform", "external_id", name="uq_product_per_business_platform"
        ),
        # The matching query the Demand Board runs: everything this
        # business sells, to compare an asked-for thing against.
        Index("idx_products_business", "business_id", "status"),
        Index("idx_products_title", "business_id", "title"),
    )


class ProductVariant(UUIDMixin, TimestampMixin, Base):
    """
    One buyable configuration of a Product - "Blue / XL", "500ml".

    This is the row the Demand Board actually compares against. When a
    customer asks "do you have this in blue", the useful answer depends on
    whether a Blue variant exists at all (you don't sell it), exists but
    is unavailable (you're out of stock), or exists and is in stock (your
    agent failed to answer a question it could have).

    `options` holds the source's own option names verbatim
    ({"Color": "Blue", "Size": "XL"}) rather than fixed columns, because
    option axes are per-business and unknowable in advance - a bakery's
    are "Flavour"/"Weight", a clothing brand's are "Colour"/"Size".
    """

    __tablename__ = "product_variants"

    product_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalised from Product so every variant query is business-scoped
    # without a join - the same reason Message carries business_id despite
    # customer_id already implying it.
    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )

    source_platform: Mapped[str] = mapped_column(String(30), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # The seller's own code. This is what links a variant to a line item
    # on a real order, which is what makes per-SKU RTO possible.
    sku: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # "Blue / XL" - the source's own display title for the combination.
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)

    options: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    price_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Null means "this business never told us", which is not the same as
    # zero. Only a real zero should ever read as out of stock.
    inventory_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The source's own availability flag where it has one, which is often
    # more honest than inventory maths (a made-to-order product can be
    # available at zero stock).
    available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "business_id",
            "source_platform",
            "external_id",
            name="uq_variant_per_business_platform",
        ),
        # Order line items carry a SKU; this is the index that turns them
        # into per-product analytics.
        Index("idx_variants_sku", "business_id", "sku"),
        Index("idx_variants_product", "product_id"),
    )
