"""
Importing a business's Meta (WhatsApp/Instagram) product catalogue.

This is the important one for this market. Most sellers here are not on
Shopify - they sell out of Instagram and WhatsApp DMs - but many of them
have already built a Meta catalogue, because that is what lets them show
products inside a chat. So for a large share of businesses the catalogue
already exists and needs no setup at all: they configured `catalog_id`
once (services/api/routers/channels.py::set_catalog_id) and Krova can read
it with the same token it already uses to send catalog messages.

Reuses WhatsAppClient.list_catalog_products rather than re-implementing the
Graph call. That method requests exactly these fields:

    name, retailer_id, price, image_url, availability

**A real shape difference, not smoothed over**: a Meta catalogue is flat.
Those fields carry no product/variant hierarchy - "Kurta Blue XL" is its
own item, not a variant of "Kurta". So each catalogue item becomes one
Product with one Variant, and that variant's SKU is Meta's `retailer_id`,
which is the business's own product code and therefore the thing most
likely to match a line item on a real order. Inventing a hierarchy Meta
never gave us would be guessing.

Meta's `price` comes back as a display string ("₹499.00", "499.00 INR")
rather than a number, and the exact format was not confirmed against a live
catalogue before writing this. Parsing is therefore deliberately defensive
and logs once when it cannot read a price, so a real format mismatch shows
up in production logs rather than silently writing nulls - the same
discipline shared/care/shiprocket_sync.py applies to its own unconfirmed
field names.
"""

import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt
from shared.catalogue.sync import IncomingProduct, IncomingVariant, SyncSummary, upsert_product
from shared.db.models import Channel, ChannelConnection, ConnectionStatus
from shared.utils.logging import get_logger

logger = get_logger(__name__)

SOURCE = "meta_catalog"

# "₹499.00", "499.00 INR", "1,299" - take the first number-ish run and
# treat it as major currency units.
_PRICE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


def _price_paise(value) -> int | None:
    """Meta's display price string -> paise, or None if unreadable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(round(float(value) * 100))
    match = _PRICE.search(str(value))
    if match is None:
        return None
    try:
        return int(round(float(match.group(1).replace(",", "")) * 100))
    except ValueError:
        return None


def _available(value) -> bool | None:
    """
    Meta's availability string -> bool, or None when it says something
    this doesn't recognise. Unknown must never read as out of stock.
    """
    if value is None:
        return None
    text = str(value).strip().lower().replace("_", " ")
    if text in {"in stock", "available", "available for order", "preorder"}:
        return True
    if text in {"out of stock", "discontinued", "unavailable"}:
        return False
    return None


def to_incoming(item: dict) -> IncomingProduct | None:
    """One Meta catalogue item -> the shared import shape."""
    external_id = item.get("id") or item.get("retailer_id")
    name = item.get("name")
    if not external_id or not name:
        return None

    external_id = str(external_id)
    retailer_id = item.get("retailer_id")
    price_paise = _price_paise(item.get("price"))
    if price_paise is None and item.get("price") is not None:
        logger.warning(
            "meta catalogue price not parseable (%r) - product kept without a price",
            item.get("price"),
        )

    return IncomingProduct(
        external_id=external_id,
        title=name,
        image_url=item.get("image_url"),
        raw=item,
        variants=[
            IncomingVariant(
                # Flat catalogue: the item is its own only variant.
                external_id=external_id,
                sku=str(retailer_id) if retailer_id else None,
                title=name,
                price_paise=price_paise,
                available=_available(item.get("availability")),
                raw=item,
            )
        ],
    )


async def sync_business(
    db: AsyncSession, *, business_id: uuid.UUID, limit: int = 200
) -> SyncSummary:
    """
    Pull this business's Meta catalogue into the local mirror.

    Returns an empty summary - not an error - when the business has no
    WhatsApp connection or has never configured a catalog_id. Both are the
    normal, common state and neither is a failure worth raising over.
    """
    summary = SyncSummary()

    connection = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == business_id,
                ChannelConnection.channel == Channel.whatsapp,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()

    if connection is None or not connection.access_token:
        return summary

    catalog_id = (connection.extra or {}).get("catalog_id")
    if not catalog_id:
        return summary

    from shared.channels.whatsapp.client import WhatsAppClient, WhatsAppError

    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)
    try:
        items = await client.list_catalog_products(catalog_id, limit=limit)
    except WhatsAppError:
        logger.warning(
            "meta catalogue read failed for business=%s catalog=%s", business_id, catalog_id
        )
        return summary

    for item in items:
        incoming = to_incoming(item)
        if incoming is None:
            continue
        await upsert_product(
            db,
            business_id=business_id,
            source_platform=SOURCE,
            incoming=incoming,
            summary=summary,
        )

    logger.info(
        "meta catalogue synced business=%s items=%s created=%s updated=%s retired=%s",
        business_id, len(items), summary.products_created,
        summary.products_updated, summary.variants_retired,
    )
    return summary
