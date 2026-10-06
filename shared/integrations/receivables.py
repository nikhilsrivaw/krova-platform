"""
Receivables from any source, as one shape: who owes what, by when.

A file (CSV) or a connected system (Zoho) turns into ReceivableRow values, and
apply_receivables() reconciles them into the Commitment Ledger the same way for
both. Rows are keyed by the invoice number, per source, so importing the same
file twice updates rows instead of duplicating them.

Missing-means-paid is opt-in per import. It is only right when the file is the
business's full outstanding list, so a partial file cannot close real debts.
"""

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models.identity import Customer
from shared.db.models.intelligence import (
    Commitment,
    CommitmentDirection,
    CommitmentKind,
    CommitmentStatus,
)

CSV_COLUMNS = ("customer", "invoice_number", "due_date", "amount")
MAX_ROWS = 5000


@dataclass(slots=True)
class ReceivableRow:
    customer: str
    invoice_number: str
    due_date: date | None
    amount_paise: int


@dataclass(slots=True)
class RowError:
    line: int
    reason: str


def parse_csv(content: bytes) -> tuple[list[ReceivableRow], list[RowError]]:
    """Read a CSV with the columns customer, invoice_number, due_date, amount."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], [RowError(0, "The file is not UTF-8 text. Save it as CSV (UTF-8) and try again.")]

    reader = csv.DictReader(io.StringIO(text))
    headers = {(h or "").strip().lower() for h in (reader.fieldnames or [])}
    missing = [c for c in CSV_COLUMNS if c not in headers]
    if missing:
        return [], [RowError(1, f"Missing column(s): {', '.join(missing)}")]

    rows: list[ReceivableRow] = []
    errors: list[RowError] = []
    for line, raw in enumerate(reader, start=2):
        if len(rows) + len(errors) >= MAX_ROWS:
            errors.append(RowError(line, f"More than {MAX_ROWS} rows. Split the file and import in parts."))
            break
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        if not any(row.values()):
            continue
        parsed, reason = _parse_row(row)
        if parsed is None:
            errors.append(RowError(line, reason))
        else:
            rows.append(parsed)
    return rows, errors


def _parse_row(row: dict) -> tuple[ReceivableRow | None, str]:
    customer = row.get("customer", "")
    invoice = row.get("invoice_number", "")
    if not customer:
        return None, "Customer name is empty"
    if not invoice:
        return None, "Invoice number is empty"
    try:
        amount = Decimal(row.get("amount", "").replace(",", ""))
    except (InvalidOperation, ValueError):
        return None, f"Amount '{row.get('amount', '')}' is not a number"
    if amount <= 0:
        return None, "Amount must be more than zero"
    due = _parse_date(row.get("due_date", ""))
    if row.get("due_date") and due is None:
        return None, f"Due date '{row['due_date']}' is not YYYY-MM-DD or DD/MM/YYYY"
    return ReceivableRow(customer, invoice, due, int(amount * 100)), ""


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


async def apply_receivables(
    db: AsyncSession,
    business_id,
    source: str,
    rows: list[ReceivableRow],
    *,
    mark_missing_paid: bool,
) -> dict:
    now = datetime.now(UTC)
    counts = {"created": 0, "updated": 0, "reopened": 0, "resolved": 0, "rows": len(rows)}
    seen: set[str] = set()

    for item in rows:
        seen.add(item.invoice_number)
        customer = await _customer_for(db, business_id, item.customer, now)
        due_at = datetime.combine(item.due_date, time.min, tzinfo=UTC) if item.due_date else None
        description = f"Invoice {item.invoice_number} - {item.customer}"

        row = (
            await db.execute(
                select(Commitment).where(
                    Commitment.business_id == business_id,
                    Commitment.source_system == source,
                    Commitment.external_ref == item.invoice_number,
                )
            )
        ).scalars().first()

        if row is None:
            db.add(Commitment(
                business_id=business_id,
                customer_id=customer.id,
                direction=CommitmentDirection.they_owe,
                kind=CommitmentKind.payment,
                description=description,
                amount_paise=item.amount_paise,
                currency="INR",
                due_at=due_at,
                due_at_explicit=True,
                status=CommitmentStatus.open,
                confidence=1.0,
                source_message_ids=[],
                source_system=source,
                external_ref=item.invoice_number,
            ))
            counts["created"] += 1
            continue

        if row.status != CommitmentStatus.open:
            row.status = CommitmentStatus.open
            row.resolved_at = None
            counts["reopened"] += 1
        row.customer_id = customer.id
        row.description = description
        row.amount_paise = item.amount_paise
        row.due_at = due_at
        counts["updated"] += 1

    if mark_missing_paid:
        open_rows = (
            await db.execute(
                select(Commitment).where(
                    Commitment.business_id == business_id,
                    Commitment.source_system == source,
                    Commitment.status == CommitmentStatus.open,
                )
            )
        ).scalars().all()
        for row in open_rows:
            if row.external_ref not in seen:
                row.status = CommitmentStatus.met
                row.resolved_at = now
                counts["resolved"] += 1

    await db.flush()
    return counts


async def _customer_for(db: AsyncSession, business_id, name: str, now: datetime) -> Customer:
    existing = (
        await db.execute(
            select(Customer).where(
                Customer.business_id == business_id,
                func.lower(Customer.display_name) == name.lower(),
            )
        )
    ).scalars().first()
    if existing is not None:
        return existing
    customer = Customer(business_id=business_id, display_name=name, first_seen_at=now, last_contact_at=now)
    db.add(customer)
    await db.flush()
    return customer
