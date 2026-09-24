"""
Importing a catalogue from a CSV a business exported from its own site.

The path for the segment nothing else reaches: sellers running their own
website - WooCommerce, Dukaan, something custom - who have no webhook to
offer and no Meta catalogue. Every one of those platforms can export a
spreadsheet, so a spreadsheet is the lowest common denominator.

**Deliberately forgiving about column names.** A Shopify export says
"Variant SKU", WooCommerce says "SKU", a hand-made sheet might say "Item
Code". Demanding one exact format would mean the business editing their
file before it works, which in practice means they don't. So headers are
normalised and matched against a list of aliases, and anything
unrecognised is ignored rather than fatal.

**Multiple rows can be one product.** Shopify's own export writes one row
per variant, all sharing a Handle - so rows are grouped by handle (or by
title when there is no handle), and each row in a group becomes a variant.
A flat one-row-per-product sheet still works: it just produces one
variant each.

Re-importing the same file updates rather than duplicates, because the
external_id is derived from the product key in the file rather than
generated fresh - the same idempotency property the Shopify webhook gets
from Shopify's own ids.
"""

import csv
import io
import re

from shared.catalogue.sync import IncomingProduct, IncomingVariant
from shared.utils.logging import get_logger

logger = get_logger(__name__)

SOURCE = "csv"

# Rows beyond this are ignored rather than accepted slowly - a catalogue
# larger than this is not being managed in a spreadsheet.
MAX_ROWS = 10_000

_PRICE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


def _norm(header: str) -> str:
    """'Variant SKU' / 'variant_sku' / 'VARIANT-SKU' -> 'variantsku'."""
    return re.sub(r"[^a-z0-9]", "", (header or "").strip().lower())


# Canonical field -> the header spellings seen in real exports.
_ALIASES: dict[str, tuple[str, ...]] = {
    "handle": ("handle", "slug", "permalink", "producthandle"),
    "title": ("title", "name", "productname", "producttitle", "item", "itemname"),
    "sku": ("variantsku", "sku", "itemcode", "productcode", "barcode"),
    "price": ("variantprice", "regularprice", "price", "sellingprice", "mrp", "rate"),
    "quantity": (
        "variantinventoryqty", "inventoryqty", "stock", "quantity", "qty",
        "instock", "inventory", "stockquantity",
    ),
    "product_type": ("type", "producttype", "category", "categories", "productcategory"),
    "vendor": ("vendor", "brand", "manufacturer", "supplier"),
    "image": ("imagesrc", "image", "images", "imageurl", "productimage"),
    "description": ("bodyhtml", "body", "description", "productdescription", "shortdescription"),
    "variant_title": ("varianttitle", "variantname", "variation"),
}


def _price_paise(value: str | None) -> int | None:
    if not value:
        return None
    match = _PRICE.search(str(value))
    if match is None:
        return None
    try:
        return int(round(float(match.group(1).replace(",", "")) * 100))
    except ValueError:
        return None


def _quantity(value: str | None) -> int | None:
    """Stock as an int, or None when the sheet doesn't say."""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().lower()
    if text in {"yes", "instock", "in stock", "true"}:
        return None  # a yes/no column tells availability, not a count
    if text in {"no", "outofstock", "out of stock", "false"}:
        return 0
    try:
        return int(float(text.replace(",", "")))
    except ValueError:
        return None


def _build_map(fieldnames: list[str]) -> dict[str, str]:
    """Canonical field -> the actual header in this file."""
    normalised = {_norm(f): f for f in fieldnames if f}
    found: dict[str, str] = {}
    for canonical, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in normalised:
                found[canonical] = normalised[alias]
                break
    return found


def _options(row: dict, fieldnames: list[str]) -> dict:
    """
    Shopify-style 'Option1 Name' / 'Option1 Value' pairs -> {name: value}.

    Falls back to nothing rather than guessing: a column called "Colour"
    on its own could equally be a product attribute the seller never sells
    variants on, and inventing an option axis would make the Demand Board
    claim variants that don't exist.
    """
    normalised = {_norm(f): f for f in fieldnames if f}
    options: dict[str, str] = {}
    for index in (1, 2, 3):
        name_col = normalised.get(f"option{index}name")
        value_col = normalised.get(f"option{index}value")
        if not name_col or not value_col:
            continue
        name = (row.get(name_col) or "").strip()
        value = (row.get(value_col) or "").strip()
        if name and value:
            options[name] = value
    return options


def parse_catalogue_csv(raw: bytes) -> tuple[list[IncomingProduct], list[str]]:
    """
    A CSV file -> products ready for upsert, plus human-readable warnings.

    Warnings are returned rather than raised so a mostly-good file still
    imports: a business with 300 products and 4 bad rows wants the 296.
    """
    warnings: list[str] = []

    # utf-8-sig because a sheet saved from Excel carries a BOM, which
    # would otherwise corrupt the very first header and break the mapping.
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
            warnings.append("File wasn't UTF-8; read as Latin-1. Check for odd characters.")
        except UnicodeDecodeError:
            return [], ["Could not read this file as text. Export it as CSV and try again."]

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = list(reader.fieldnames or [])
    if not fieldnames:
        return [], ["The file has no header row."]

    mapping = _build_map(fieldnames)
    if "title" not in mapping and "handle" not in mapping:
        return [], [
            "Couldn't find a product name column. Expected one called "
            "'Title', 'Name' or 'Handle'."
        ]

    grouped: dict[str, IncomingProduct] = {}
    skipped = 0

    for index, row in enumerate(reader):
        if index >= MAX_ROWS:
            warnings.append(f"Only the first {MAX_ROWS} rows were read.")
            break

        title = (row.get(mapping.get("title", "")) or "").strip()
        handle = (row.get(mapping.get("handle", "")) or "").strip()
        key = handle or title
        if not key:
            skipped += 1
            continue

        product = grouped.get(key)
        if product is None:
            product = IncomingProduct(
                external_id=key[:120],
                title=title or handle,
                product_type=(row.get(mapping.get("product_type", "")) or "").strip() or None,
                vendor=(row.get(mapping.get("vendor", "")) or "").strip() or None,
                description=(row.get(mapping.get("description", "")) or "").strip() or None,
                image_url=(row.get(mapping.get("image", "")) or "").strip() or None,
                raw={},
                variants=[],
            )
            grouped[key] = product
        elif title and not product.title:
            product.title = title

        options = _options(row, fieldnames)
        sku = (row.get(mapping.get("sku", "")) or "").strip() or None
        variant_title = (row.get(mapping.get("variant_title", "")) or "").strip() or None
        if not variant_title and options:
            variant_title = " / ".join(options.values())

        quantity = _quantity(row.get(mapping.get("quantity", "")))
        product.variants.append(
            IncomingVariant(
                # Stable across re-imports: the SKU where there is one,
                # otherwise position within this product's rows.
                external_id=(sku or f"{key}:{len(product.variants)}")[:120],
                sku=sku,
                title=variant_title or product.title,
                options=options,
                price_paise=_price_paise(row.get(mapping.get("price", ""))),
                inventory_quantity=quantity,
                available=(quantity > 0) if quantity is not None else None,
                raw={},
            )
        )

    if skipped:
        warnings.append(f"{skipped} row(s) had no product name and were skipped.")

    products = [p for p in grouped.values() if p.variants]
    if not products:
        warnings.append("No usable products found in the file.")

    return products, warnings
