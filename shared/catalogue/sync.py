"""
One upsert, many sources.

Products arrive from a Shopify webhook, a Meta catalogue read, a CSV a
business exported from its own site, or typed in by hand. All four end at
`upsert_product` so the catalogue has one shape regardless of origin, and
so adding the next source is a parser rather than a second write path.

**On mirroring, and why this exists at all.** WhatsAppClient.
list_catalog_products deliberately does *not* cache - its docstring is
explicit that reading live is what keeps it from going stale, and for
answering a customer right now that is correct. This module is for the
other question: analytics over time. "47 people asked for Blue in the last
30 days and you have never stocked Blue" cannot be answered by a live read,
because a live read only knows about now. Both are right for their own job;
the live reader stays the source of truth for conversation, this mirror is
the source of history.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Product, ProductVariant
from shared.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class IncomingVariant:
    """One buyable configuration, as some source described it."""

    external_id: str
    sku: str | None = None
    title: str | None = None
    options: dict = field(default_factory=dict)
    price_paise: int | None = None
    inventory_quantity: int | None = None
    available: bool | None = None
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class IncomingProduct:
    """A product plus its variants, normalised away from any one platform."""

    external_id: str
    title: str
    variants: list[IncomingVariant]
    product_type: str | None = None
    vendor: str | None = None
    description: str | None = None
    status: str = "active"
    image_url: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class SyncSummary:
    products_created: int = 0
    products_updated: int = 0
    variants_created: int = 0
    variants_updated: int = 0
    variants_retired: int = 0


async def upsert_product(
    db: AsyncSession,
    *,
    business_id: uuid.UUID,
    source_platform: str,
    incoming: IncomingProduct,
    summary: SyncSummary | None = None,
) -> Product:
    """
    Create or update one product and its variants. Caller commits.

    A variant that has vanished from the source is marked unavailable, not
    deleted - an existing order line item may still point at it, and "you
    used to sell this" is a real answer to a customer asking for it.
    """
    summary = summary or SyncSummary()

    product = (
        await db.execute(
            select(Product).where(
                Product.business_id == business_id,
                Product.source_platform == source_platform,
                Product.external_id == incoming.external_id,
            )
        )
    ).scalars().first()

    if product is None:
        product = Product(
            business_id=business_id,
            source_platform=source_platform,
            external_id=incoming.external_id,
        )
        db.add(product)
        summary.products_created += 1
    else:
        summary.products_updated += 1

    product.title = incoming.title
    product.product_type = incoming.product_type
    product.vendor = incoming.vendor
    product.description = incoming.description
    product.status = incoming.status
    product.image_url = incoming.image_url
    product.raw_payload = incoming.raw
    await db.flush()

    existing = {
        v.external_id: v
        for v in (
            await db.execute(
                select(ProductVariant).where(ProductVariant.product_id == product.id)
            )
        ).scalars().all()
    }

    now = datetime.now(timezone.utc)
    seen: set[str] = set()

    for item in incoming.variants:
        seen.add(item.external_id)
        variant = existing.get(item.external_id)
        if variant is None:
            variant = ProductVariant(
                product_id=product.id,
                business_id=business_id,
                source_platform=source_platform,
                external_id=item.external_id,
            )
            db.add(variant)
            summary.variants_created += 1
        else:
            summary.variants_updated += 1

        variant.sku = item.sku
        variant.title = item.title
        variant.options = item.options
        variant.price_paise = item.price_paise
        variant.inventory_quantity = item.inventory_quantity
        variant.available = item.available
        variant.synced_at = now
        variant.raw_payload = item.raw

    for external_id, variant in existing.items():
        if external_id not in seen and variant.available is not False:
            variant.available = False
            variant.synced_at = now
            summary.variants_retired += 1

    return product
