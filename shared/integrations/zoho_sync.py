"""
Pull a business's open Zoho invoices into the ledger, through the shared
receivables reconciliation (shared/integrations/receivables.py).

Zoho drops paid and voided invoices from the open list alike, so both end up
as met. That is the known limit of this read.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt
from shared.db.models.zoho import ZohoConnection
from shared.integrations import zoho_books
from shared.integrations.receivables import ReceivableRow, apply_receivables

SOURCE = "zoho"


async def sync_connection(db: AsyncSession, connection: ZohoConnection) -> dict:
    access = await zoho_books.access_token_from_refresh(decrypt(connection.refresh_token))
    invoices = await zoho_books.list_open_invoices(access, connection.organization_id)
    rows = [
        ReceivableRow(
            customer=inv.customer_name,
            invoice_number=inv.invoice_id,
            due_date=inv.due_date,
            amount_paise=inv.balance_paise,
        )
        for inv in invoices
    ]
    counts = await apply_receivables(db, connection.business_id, SOURCE, rows, mark_missing_paid=True)
    counts["open_invoices"] = len(invoices)
    connection.last_synced_at = datetime.now(UTC)
    connection.last_sync_summary = counts
    await db.flush()
    return counts
