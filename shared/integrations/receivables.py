"""
Receivables from any source, as one shape: who owes what, by when.

A file (CSV or Excel) or a connected system (Zoho) turns into ReceivableRow
values, and apply_receivables() reconciles them into the Commitment Ledger the
same way for both. Rows are keyed by the invoice number, per source, so
importing the same file twice updates rows instead of duplicating them.

Column names differ across accounting software. Each field accepts the common
labels below, matched case-insensitively. An export whose labels are not in
this list needs its headers renamed once, or a new alias added here.

Missing-means-paid is opt-in per import. It is only right when the file is the
business's full outstanding list, so a partial file cannot close real debts.
"""

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation

import openpyxl
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models.identity import Customer
from shared.db.models.intelligence import (
    Commitment,
    CommitmentDirection,
    CommitmentKind,
    CommitmentStatus,
)

# Field -> the labels it is recognised by (lowercase). The first is the template's own name.
ALIASES: dict[str, tuple[str, ...]] = {
    "customer": ("customer", "customer name", "party", "party name", "ledger name", "name"),
    "invoice_number": (
        "invoice_number", "invoice number", "invoice no", "invoice no.", "invoice #",
        "bill no", "bill no.", "bill number", "voucher no", "voucher number",
    ),
    "due_date": ("due_date", "due date", "due on", "due"),
    "amount": (
        "amount", "balance", "balance due", "amount due", "outstanding",
        "closing balance", "total",
    ),
}
REQUIRED = ("customer", "invoice_number", "amount")
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


def parse_file(filename: str, content: bytes) -> tuple[list[ReceivableRow], list[RowError]]:
    lowered = filename.lower()
    if lowered.endswith(".xlsx"):
        headers, records = _read_xlsx(content)
    else:
        headers, records = _read_csv(content)
    return parse_records(headers, records)


def _read_csv(content: bytes) -> tuple[list[str], list[dict]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("The file is not UTF-8 text. Save it as CSV (UTF-8) and try again.") from exc
    reader = csv.DictReader(io.StringIO(text))
    return list(reader.fieldnames or []), list(reader)


def _read_xlsx(content: bytes) -> tuple[list[str], list[dict]]:
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError("This is not a readable Excel file. Save it again as .xlsx.") from exc
    sheet = workbook.worksheets[0]
    iterator = sheet.iter_rows(values_only=True)
    header_row = next(iterator, None) or ()
    headers = [str(h).strip() if h is not None else "" for h in header_row]
    records = []
    for values in iterator:
        records.append({headers[i]: values[i] for i in range(min(len(headers), len(values))) if headers[i]})
    workbook.close()
    return headers, records


def parse_records(headers: list[str], records: list[dict]) -> tuple[list[ReceivableRow], list[RowError]]:
    column = _resolve_columns(headers)
    missing = [field for field in REQUIRED if field not in column]
    if missing:
        return [], [RowError(1, f"Missing column(s): {', '.join(missing)}. Accepted names: see the template.")]

    rows: list[ReceivableRow] = []
    errors: list[RowError] = []
    for line, record in enumerate(records, start=2):
        if len(rows) + len(errors) >= MAX_ROWS:
            errors.append(RowError(line, f"More than {MAX_ROWS} rows. Split the file and import in parts."))
            break
        values = {field: record.get(header) for field, header in column.items()}
        if all(_blank(v) for v in values.values()):
            continue
        parsed, reason = _parse_row(values)
        if parsed is None:
            errors.append(RowError(line, reason))
        else:
            rows.append(parsed)
    return rows, errors


def _resolve_columns(headers: list[str]) -> dict[str, str]:
    """Field -> the file's own header, for the first alias each field matches."""
    by_label = {}
    for header in headers:
        key = (header or "").strip().lower()
        if key:
            by_label.setdefault(key, header)
    resolved = {}
    for field, labels in ALIASES.items():
        for label in labels:
            if label in by_label:
                resolved[field] = by_label[label]
                break
    return resolved


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _parse_row(values: dict) -> tuple[ReceivableRow | None, str]:
    customer = str(values.get("customer") or "").strip()
    invoice = str(values.get("invoice_number") or "").strip()
    if not customer:
        return None, "Customer name is empty"
    if not invoice:
        return None, "Invoice number is empty"

    raw_amount = values.get("amount")
    try:
        amount = Decimal(str(raw_amount).replace(",", "").strip()) if not _blank(raw_amount) else Decimal(0)
    except (InvalidOperation, ValueError):
        return None, f"Amount '{raw_amount}' is not a number"
    if amount <= 0:
        return None, "Amount must be more than zero"

    raw_due = values.get("due_date")
    due = _as_date(raw_due)
    if not _blank(raw_due) and due is None:
        return None, f"Due date '{raw_due}' is not YYYY-MM-DD or DD/MM/YYYY"
    return ReceivableRow(customer, invoice, due, int(amount * 100)), ""


def _as_date(value) -> date | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
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
