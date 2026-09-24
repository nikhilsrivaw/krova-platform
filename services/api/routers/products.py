"""
The product catalogue: read it, and get it in from wherever it lives.

Kept separate from orders.py because a product is not an order, and
because where products come from differs completely by business. Most
sellers in this market are not on Shopify - they run on WhatsApp and
Instagram, or on their own site - so this router leads with the paths that
serve them: a one-click Meta catalogue import for anyone who already shows
products in chat, and plain manual entry for a seller whose catalogue has
only ever existed in their own head.
"""

import uuid

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared import verticals
from shared.catalogue import csv_import, meta_catalog
from shared.catalogue.sync import IncomingProduct, IncomingVariant, SyncSummary, upsert_product
from shared.db.models import Business, Product, ProductVariant

router = APIRouter(prefix="/products", tags=["products"])

# A catalogue bigger than this is not being managed in a spreadsheet.
MAX_IMPORT_BYTES = 5 * 1024 * 1024


async def _require_order_sync(business_id: uuid.UUID, db: DbDep) -> Business:
    """Same self-gating shape as orders.py::_require_order_sync."""
    business = await db.get(Business, business_id)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    if not verticals.has_capability(business, "order_sync"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This business does not have the order_sync capability"
        )
    return business


class VariantOut(BaseModel):
    id: uuid.UUID
    sku: str | None
    title: str | None
    options: dict
    price_paise: int | None
    inventory_quantity: int | None
    available: bool | None


class ProductOut(BaseModel):
    id: uuid.UUID
    source_platform: str
    title: str
    product_type: str | None
    vendor: str | None
    status: str
    image_url: str | None
    variants: list[VariantOut]


class SyncResult(BaseModel):
    products_created: int
    products_updated: int
    variants_created: int
    variants_updated: int
    variants_retired: int
    note: str


class ImportResult(BaseModel):
    products_created: int
    products_updated: int
    variants_created: int
    variants_updated: int
    variants_retired: int
    # Surfaced, not swallowed: a file that half-worked should say so.
    warnings: list[str]
    note: str


class VariantIn(BaseModel):
    sku: str | None = None
    title: str | None = None
    options: dict = Field(default_factory=dict)
    price_paise: int | None = None
    inventory_quantity: int | None = None
    available: bool | None = None


class ProductIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    product_type: str | None = None
    vendor: str | None = None
    description: str | None = None
    image_url: str | None = None
    variants: list[VariantIn] = Field(default_factory=list)


def _to_out(product: Product, variants: list[ProductVariant]) -> ProductOut:
    return ProductOut(
        id=product.id,
        source_platform=product.source_platform,
        title=product.title,
        product_type=product.product_type,
        vendor=product.vendor,
        status=product.status,
        image_url=product.image_url,
        variants=[
            VariantOut(
                id=v.id,
                sku=v.sku,
                title=v.title,
                options=v.options or {},
                price_paise=v.price_paise,
                inventory_quantity=v.inventory_quantity,
                available=v.available,
            )
            for v in variants
        ],
    )


@router.get("", response_model=list[ProductOut])
async def list_products(
    current_user: CurrentUserDep,
    db: DbDep,
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, le=500),
) -> list[ProductOut]:
    """Everything this business sells, whatever source it came from."""
    await _require_order_sync(current_user.business, db)

    query = select(Product).where(Product.business_id == current_user.business)
    if search:
        query = query.where(Product.title.ilike(f"%{search}%"))
    products = (
        await db.execute(query.order_by(Product.title).limit(limit))
    ).scalars().all()
    if not products:
        return []

    variants = (
        await db.execute(
            select(ProductVariant)
            .where(ProductVariant.product_id.in_([p.id for p in products]))
            .order_by(ProductVariant.title)
        )
    ).scalars().all()

    grouped: dict[uuid.UUID, list[ProductVariant]] = {}
    for variant in variants:
        grouped.setdefault(variant.product_id, []).append(variant)

    return [_to_out(p, grouped.get(p.id, [])) for p in products]


@router.post("/sync/meta", response_model=SyncResult)
async def sync_meta_catalogue(current_user: CurrentUserDep, db: DbDep) -> SyncResult:
    """
    Import the business's Meta (WhatsApp/Instagram) catalogue.

    For a seller who already shows products in chat this is the whole
    setup - the catalogue exists, and the same token that sends catalog
    messages can read it. Returns zeros rather than an error when there is
    no WhatsApp connection or no catalog_id configured, because both are
    ordinary states rather than failures.
    """
    await _require_order_sync(current_user.business, db)
    summary = await meta_catalog.sync_business(db, business_id=current_user.business)
    await db.commit()

    imported = summary.products_created + summary.products_updated
    return SyncResult(
        products_created=summary.products_created,
        products_updated=summary.products_updated,
        variants_created=summary.variants_created,
        variants_updated=summary.variants_updated,
        variants_retired=summary.variants_retired,
        note=(
            "Imported from your Meta catalogue."
            if imported
            else "Nothing imported - connect WhatsApp and set a catalog ID first."
        ),
    )


@router.post("/import", response_model=ImportResult)
async def import_catalogue_csv(
    current_user: CurrentUserDep,
    db: DbDep,
    file: UploadFile = File(...),
) -> ImportResult:
    """
    Import a catalogue from a CSV the business exported from its own site.

    The path for sellers on their own website - WooCommerce, Dukaan,
    something custom - who have no webhook to offer and no Meta catalogue.
    Column names are matched forgivingly (see shared/catalogue/csv_import.py),
    and a file with some bad rows still imports the good ones rather than
    failing wholesale.

    Re-uploading the same file updates rather than duplicates.
    """
    await _require_order_sync(current_user.business, db)

    raw = await file.read()
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The file is empty.")
    if len(raw) > MAX_IMPORT_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"File is larger than {MAX_IMPORT_BYTES // (1024 * 1024)}MB.",
        )

    products, warnings = csv_import.parse_catalogue_csv(raw)
    if not products:
        return ImportResult(
            products_created=0, products_updated=0, variants_created=0,
            variants_updated=0, variants_retired=0, warnings=warnings,
            note="Nothing was imported.",
        )

    summary = SyncSummary()
    for incoming in products:
        await upsert_product(
            db,
            business_id=current_user.business,
            source_platform=csv_import.SOURCE,
            incoming=incoming,
            summary=summary,
        )
    await db.commit()

    return ImportResult(
        products_created=summary.products_created,
        products_updated=summary.products_updated,
        variants_created=summary.variants_created,
        variants_updated=summary.variants_updated,
        variants_retired=summary.variants_retired,
        warnings=warnings,
        note=(
            f"{summary.products_created} added, "
            f"{summary.products_updated} updated."
        ),
    )


@router.post("", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
async def create_product(
    body: ProductIn, current_user: CurrentUserDep, db: DbDep
) -> ProductOut:
    """
    Add a product by hand.

    The path for a seller with no store platform at all - which in this
    market is common, not an edge case. A product with no variants still
    gets one, because every downstream question (was it asked for, did it
    come back, is it in stock) is asked of a variant.
    """
    await _require_order_sync(current_user.business, db)

    external_id = str(uuid.uuid4())
    variants = body.variants or [VariantIn(title=body.title)]
    incoming = IncomingProduct(
        external_id=external_id,
        title=body.title,
        product_type=body.product_type,
        vendor=body.vendor,
        description=body.description,
        image_url=body.image_url,
        raw={},
        variants=[
            IncomingVariant(
                external_id=f"{external_id}:{index}",
                sku=v.sku,
                title=v.title or body.title,
                options=v.options,
                price_paise=v.price_paise,
                inventory_quantity=v.inventory_quantity,
                available=v.available,
            )
            for index, v in enumerate(variants)
        ],
    )

    product = await upsert_product(
        db,
        business_id=current_user.business,
        source_platform="manual",
        incoming=incoming,
        summary=SyncSummary(),
    )
    await db.commit()

    rows = (
        await db.execute(
            select(ProductVariant).where(ProductVariant.product_id == product.id)
        )
    ).scalars().all()
    return _to_out(product, rows)


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(
    product_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep
) -> None:
    """Remove a manually added product. Variants cascade."""
    await _require_order_sync(current_user.business, db)
    product = await db.get(Product, product_id)
    if product is None or product.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    await db.delete(product)
    await db.commit()
