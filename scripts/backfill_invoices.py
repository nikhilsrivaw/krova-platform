"""
Make invoices and receipts for successful payments that were taken before invoicing existed.

    docker compose -f docker-compose.prod.yml exec app python -m scripts.backfill_invoices                   # list only
    docker compose -f docker-compose.prod.yml exec app python -m scripts.backfill_invoices --min-rupees 50    # skip tiny test payments
    docker compose -f docker-compose.prod.yml exec app python -m scripts.backfill_invoices --min-rupees 50 --apply

Read the list first. Every invoice takes the next number in the legal series (AQX/<year>/...), and a tax invoice
cannot be undone, so do NOT backfill payments that were only tests (PayU sandbox, the Rs 1 coupon): leave them out
with --min-rupees, or do not run --apply at all. Payments are numbered oldest first. The buyer's details are
whatever the business has saved now (or its name and the seller's state if nothing is saved).
"""
import asyncio
import sys

from sqlalchemy import select

from shared.billing import invoices
from shared.billing.service import add_month
from shared.db.models import Business, Invoice, Payment
from shared.db.session import AsyncSessionLocal


async def backfill(db, *, min_paise: int = 0, apply: bool = False) -> list[str]:
    """One line per payment that has no invoice yet. With apply, the documents are created too."""
    done = select(Invoice.payment_id)
    payments = (await db.execute(
        select(Payment).where(Payment.status == "success", Payment.id.not_in(done)).order_by(Payment.paid_at)
    )).scalars().all()
    log: list[str] = []
    for p in payments:
        what = {"topup": "receipt", "plan": "invoice", "renewal": "invoice"}.get(p.purpose, "invoice")
        line = f"{p.paid_at:%Y-%m-%d %H:%M}  {p.purpose:8s} Rs {p.total_paise / 100:>9,.2f}  {p.txnid}  -> {what}"
        if p.total_paise < min_paise:
            log.append(line + "   SKIPPED (under the minimum)")
            continue
        if apply:
            business = await db.get(Business, p.business_id)
            if business is None:
                log.append(line + "   SKIPPED (business gone)")
                continue
            start = p.paid_at
            inv = await invoices.create_for_payment(
                db, p, business=business,
                period_start=None if p.purpose == "topup" else start,
                period_end=None if p.purpose == "topup" else add_month(start),
            )
            line += f"   made {inv.number}"
        log.append(line)
    return log


async def main() -> None:
    args = sys.argv[1:]
    min_paise = 0
    if "--min-rupees" in args:
        min_paise = int(float(args[args.index("--min-rupees") + 1]) * 100)
    apply = "--apply" in args
    async with AsyncSessionLocal() as db:
        log = await backfill(db, min_paise=min_paise, apply=apply)
        for line in log or ["Nothing to do: every successful payment already has its document."]:
            print(line)
        if apply:
            await db.commit()
            print("\nDone.")
        else:
            print("\nList only. Add --apply to create these (read the note at the top of this file first).")


if __name__ == "__main__":
    asyncio.run(main())
