"""
Put one business in (or out of) the "has not chosen a plan yet" state, to see what a brand-new customer sees.

    docker compose -f docker-compose.prod.yml exec app python -m scripts.billing_gate "<business name>" on
    docker compose -f docker-compose.prod.yml exec app python -m scripts.billing_gate "<business name>" off
    docker compose -f docker-compose.prod.yml exec app python -m scripts.billing_gate "<business name>"      # just show

`on`  : that business is treated as new: WhatsApp, Instagram and the Voice Agent lock, the pricing shows,
        AI replies and campaigns refuse. A business that already has a subscription is not affected.
`off` : back to normal (it follows BILLING_ENFORCED_FROM like everyone else).

Only the matching business changes. The name is matched as a case-insensitive substring and must match one.
"""
import asyncio
import sys

from sqlalchemy import select

from shared.billing import guards
from shared.db.models import Business, Subscription
from shared.db.session import AsyncSessionLocal


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    needle = sys.argv[1].lower()
    action = sys.argv[2].lower() if len(sys.argv) > 2 else None

    async with AsyncSessionLocal() as db:
        rows = [b for b in (await db.execute(select(Business))).scalars().all() if needle in (b.name or "").lower()]
        if len(rows) != 1:
            print(f"{len(rows)} businesses match {needle!r}: {[b.name for b in rows]} - be more specific.")
            return
        business = rows[0]
        if action in ("on", "off"):
            settings = dict(business.settings or {})
            if action == "on":
                settings["billing_required"] = True
            else:
                settings.pop("billing_required", None)
            business.settings = settings
            await db.commit()
        has_plan = await db.get(Subscription, business.id) is not None
        state = await guards.plan_state(db, business.id)
        print(f"{business.name}: billing_required={bool((business.settings or {}).get('billing_required'))}, "
              f"has a subscription={has_plan}, plan state now={state or 'fine (not locked)'}")


if __name__ == "__main__":
    asyncio.run(main())
