"""
Read-only sanity check for the Type 1/Type 2 ledger additions
(docs/new/type-1-backlog-built.md, type-2-installments-built.md) - runs
directly against the real database, no HTTP, no auth token needed.

    docker compose -f docker-compose.prod.yml exec app python -m scripts.verify_new_ledger_features

Writes nothing. Picks the first active business with data and reports
what each new query returns for it.
"""
import asyncio

from sqlalchemy import select

from shared.care import ledger_queries as lq
from shared.db.models import Business
from shared.db.session import AsyncSessionLocal


async def main() -> None:
    async with AsyncSessionLocal() as db:
        businesses = (
            await db.execute(select(Business).where(Business.is_active == True).limit(20))  # noqa: E712
        ).scalars().all()
        if not businesses:
            print("no active businesses found")
            return

        for business in businesses:
            delivery = await lq.open_delivery_promises(business.id, db)
            missing = await lq.quotations_missing_advance(business.id, db)
            price = await lq.price_inconsistencies(business.id, db)
            plans = await lq.installment_plans(business.id, db)
            total = len(delivery) + len(missing) + len(price) + len(plans)
            if total == 0:
                continue
            print(f"\n=== {business.name} ({business.vertical}) — business_id={business.id} ===")
            if delivery:
                print(f"  delivery promises open: {len(delivery)}, overdue: {sum(r.overdue for r in delivery)}")
                for r in delivery[:3]:
                    print(f"    - {r.customer_name}: {r.description[:60]!r} due={r.due_at} overdue={r.overdue}")
            if missing:
                print(f"  won quotes missing an advance: {len(missing)}")
                for r in missing[:3]:
                    print(f"    - {r.customer_name}: {r.reference} total={r.total_paise}")
            if price:
                print(f"  price inconsistencies (same variant, same tier, different price): {len(price)}")
                for r in price[:3]:
                    prices = sorted({p for *_, p in r.quotes})
                    print(f"    - {r.variant_title!r} tier={r.price_tier!r}: prices seen {prices}")
            if plans:
                print(f"  installment plans: {len(plans)}")
                for r in plans[:3]:
                    print(f"    - {r.customer_name}: {r.description!r} paid {r.paid_paise}/{r.total_paise} "
                          f"({len(r.installments)} installments)")

        print("\ndone - nothing was written")


if __name__ == "__main__":
    asyncio.run(main())
