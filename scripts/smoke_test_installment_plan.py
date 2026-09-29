"""
Self-cleaning smoke test for the installment-plan feature against the real
production database: creates a real plan through the actual endpoint
function (not a reimplementation), reads it back, then deletes it.
Nothing is left behind.

    docker compose -f docker-compose.prod.yml exec app python -m scripts.smoke_test_installment_plan

Picks the first active business that has at least one customer. Prints
what it did at every step so a failure is easy to place.
"""
import asyncio
import uuid
from types import SimpleNamespace as NS

from sqlalchemy import select

from services.api.routers import ledger
from shared.care import ledger_queries as lq
from shared.db.models import Business, Commitment, Customer, InstallmentPlan
from shared.db.session import AsyncSessionLocal


async def main() -> None:
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(Business, Customer)
                .join(Customer, Customer.business_id == Business.id)
                .where(Business.is_active == True)  # noqa: E712
                .limit(1)
            )
        ).first()
        if row is None:
            print("no active business with a customer found - nothing to test against")
            return
        business, customer = row
        print(f"using business={business.name!r} customer={customer.display_name!r}")

        user = NS(business=business.id)
        body = ledger.InstallmentPlanIn(
            customer_id=customer.id,
            description="[SMOKE TEST - auto-deleted] deploy verification",
            installments=[
                ledger.InstallmentIn(amount_paise=100),
                ledger.InstallmentIn(amount_paise=100),
            ],
        )
        created = await ledger.create_installment_plan(body, user, db)
        plan_id = uuid.UUID(created.plan_id)
        print(f"created plan_id={plan_id} total_paise={created.total_paise} "
              f"installments={len(created.installments)}")
        assert created.total_paise == 200 and len(created.installments) == 2

        rows = await lq.installment_plans(business.id, db, customer_id=customer.id)
        found = next((r for r in rows if r.plan_id == plan_id), None)
        assert found is not None, "the plan we just created did not come back from installment_plans()"
        assert found.total_paise == 200 and len(found.installments) == 2
        print("verified: installment_plans() reads it back correctly")

        # Cleanup - CASCADE on commitments.installment_plan_id removes the
        # two Commitment rows when the plan itself is deleted.
        plan = await db.get(InstallmentPlan, plan_id)
        await db.delete(plan)
        await db.commit()

        remaining = (
            await db.execute(select(Commitment.id).where(Commitment.installment_plan_id == plan_id))
        ).scalars().all()
        still_there = await db.get(InstallmentPlan, plan_id)
        assert not remaining and still_there is None
        print("cleaned up: plan and both installments deleted, nothing left behind")

    print("\nsmoke test passed")


if __name__ == "__main__":
    asyncio.run(main())
