"""
The fee list: what each test, service or visit costs - a lab's CT scan and X-ray, a clinic's dressing, a salon's
haircut. Kept on the Knowledge page ("Fees / Price list"), the same for every business type.

Two uses:
  - Staff tick what the person came for when they make a token or a booking. The token or booking keeps its own copy
    (fee_items: [{"id", "name", "amount_paise"}]), so changing a price later never changes a bill already made, and
    the payment link for it asks for the total of what was ticked.
  - The AI answers "CT scan kitne ka hai?" from it. Like the Catalogue (shared/verticals/knowledge_lookup.py) it is
    never read whole: only the rows the customer's words name are shown, so a long list costs nothing extra.

Storage: one KnowledgeItem with topic "fees" and its rows in extra["fees"]; token_estimate 0, so it does not use the
plan's knowledge budget. Amounts are whole paise, never text, because they become a bill.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

TOPIC = "fees"
ROWS_LIMIT = 300
NAME_LIMIT = 80
DETAILS_LIMIT = 150
MIN_PAISE = 100  # ₹1
MAX_PAISE = 100_000_000  # ₹10,00,000
ITEMS_LIMIT = 20  # tests on one token or booking


class FeeError(ValueError):
    """Something staff should see, e.g. a fee that has since been removed from the list."""


def rupees(paise: int) -> str:
    whole, part = divmod(int(paise), 100)
    return f"₹{whole:,}" + (f".{part:02d}" if part else "")


def clean_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows as stored: names trimmed, amounts whole paise in range, every row with its own id, at most ROWS_LIMIT."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()[:NAME_LIMIT]
        try:
            amount = int(row.get("amount_paise"))
        except (TypeError, ValueError):
            continue
        if not name or not MIN_PAISE <= amount <= MAX_PAISE:
            continue
        row_id = str(row.get("id") or "").strip()[:20]
        if not row_id or row_id in seen:
            row_id = uuid.uuid4().hex[:10]
        seen.add(row_id)
        out.append({
            "id": row_id, "name": name, "amount_paise": amount,
            "details": str(row.get("details") or "").strip()[:DETAILS_LIMIT],
        })
    return out[:ROWS_LIMIT]


def row_text(row: dict[str, Any]) -> str:
    line = f"{row['name']} - {rupees(row['amount_paise'])}"
    return line + (f" ({row['details']})" if row.get("details") else "")


def lookup_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The rows in the Catalogue's shape, for knowledge_lookup.match_catalogue."""
    return [{"name": r["name"], "price": rupees(r["amount_paise"]), "details": r.get("details", "")} for r in rows]


async def read(db: AsyncSession, business_id: uuid.UUID) -> list[dict[str, Any]]:
    from shared.db.models import KnowledgeItem

    item = (
        await db.execute(select(KnowledgeItem).where(KnowledgeItem.business_id == business_id, KnowledgeItem.topic == TOPIC))
    ).scalar_one_or_none()
    return clean_rows((item.extra or {}).get("fees") or []) if item is not None else []


def pick(rows: list[dict[str, Any]], ids: list[str]) -> list[dict[str, Any]]:
    """What was ticked, as the token or booking keeps it. Raises FeeError for an id no longer on the list."""
    if len(ids) > ITEMS_LIMIT:
        raise FeeError(f"Pick at most {ITEMS_LIMIT}.")
    by_id = {r["id"]: r for r in rows}
    items = []
    for row_id in ids:
        row = by_id.get(row_id)
        if row is None:
            raise FeeError("One of the chosen fees is no longer on the fee list. Refresh and pick again.")
        items.append({"id": row["id"], "name": row["name"], "amount_paise": row["amount_paise"]})
    return items


async def pick_for(db: AsyncSession, business_id: uuid.UUID, ids: list[str] | None) -> list[dict[str, Any]] | None:
    """None when nothing was ticked, so a token or booking without tests stays as it always was."""
    if not ids:
        return None
    return pick(await read(db, business_id), ids)


def total(items: list[dict[str, Any]] | None) -> int:
    return sum(int(i.get("amount_paise") or 0) for i in items or [])


def label(items: list[dict[str, Any]] | None) -> str:
    """"CT scan + X-ray" - for the payment link and the ledger."""
    return " + ".join(i.get("name", "") for i in items or [] if i.get("name"))[:200]
