"""
Pull a business's open Zoho invoices into the Commitment Ledger.

Each open invoice becomes one they_owe payment commitment, keyed by Zoho's
invoice id (source_system="zoho"), so running sync again updates rows instead
of duplicating them. An invoice that was open last time and is gone now is
marked met. Zoho drops paid and voided invoices from the open list alike, so
both count as met. That is the known limit of this read.
"""

from datetime import UTC, datetime, time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt
from shared.db.models.identity import Customer
from shared.db.models.intelligence import (
    Commitment,
    CommitmentDirection,
    CommitmentKind,
    CommitmentStatus,
)
from shared.db.models.zoho import ZohoConnection
from shared.integrations import zoho_books

SOURCE = "zoho"


async def sync_connection(db: AsyncSession, connection: ZohoConnection) -> dict:
    access = await zoho_books.access_token_from_refresh(decrypt(connection.refresh_token))
    invoices = await zoho_books.list_open_invoices(access, connection.organization_id)

    now = datetime.now(UTC)
    counts = {"created": 0, "updated": 0, "reopened": 0, "resolved": 0, "open_invoices": len(invoices)}
    seen: set[str] = set()

    for invoice in invoices:
        seen.add(invoice.invoice_id)
        customer = await _customer_for(db, connection.business_id, invoice.customer_name, now)
        due_at = datetime.combine(invoice.due_date, time.min, tzinfo=UTC) if invoice.due_date else None
        description = f"Zoho invoice {invoice.invoice_number or invoice.invoice_id} - {invoice.customer_name}"

        row = (
            await db.execute(
                select(Commitment).where(
                    Commitment.business_id == connection.business_id,
                    Commitment.source_system == SOURCE,
                    Commitment.external_ref == invoice.invoice_id,
                )
            )
        ).scalars().first()

        if row is None:
            db.add(Commitment(
                business_id=connection.business_id,
                customer_id=customer.id,
                direction=CommitmentDirection.they_owe,
                kind=CommitmentKind.payment,
                description=description,
                amount_paise=invoice.balance_paise,
                currency="INR",
                due_at=due_at,
                due_at_explicit=True,
                status=CommitmentStatus.open,
                confidence=1.0,
                source_message_ids=[],
                source_system=SOURCE,
                external_ref=invoice.invoice_id,
            ))
            counts["created"] += 1
            continue

        if row.status != CommitmentStatus.open:
            row.status = CommitmentStatus.open
            row.resolved_at = None
            counts["reopened"] += 1
        row.customer_id = customer.id
        row.description = description
        row.amount_paise = invoice.balance_paise
        row.due_at = due_at
        counts["updated"] += 1

    open_rows = (
        await db.execute(
            select(Commitment).where(
                Commitment.business_id == connection.business_id,
                Commitment.source_system == SOURCE,
                Commitment.status == CommitmentStatus.open,
            )
        )
    ).scalars().all()
    for row in open_rows:
        if row.external_ref not in seen:
            row.status = CommitmentStatus.met
            row.resolved_at = now
            counts["resolved"] += 1

    connection.last_synced_at = now
    connection.last_sync_summary = counts
    await db.flush()
    return counts


async def _customer_for(db: AsyncSession, business_id, name: str, now: datetime) -> Customer:
    """Match a Zoho customer name to an existing customer, or create one."""
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
