"""
Read-only diagnostic for "changing the vertical in Settings doesn't change
the sidebar" - shows, for every active business, its stored vertical, that
vertical's template-declared capabilities, any capability_overrides in
business.settings, and the final resolved list (exactly what
verticals.capabilities_for() returns and what /auth/me sends the frontend).

    docker compose -f docker-compose.prod.yml exec app python -m scripts.debug_vertical_capabilities [business_name_substring]

Pass a substring to filter to one business (case-insensitive, matches name).
Writes nothing.
"""
import asyncio
import sys

from sqlalchemy import select

from shared import verticals
from shared.db.models import Business
from shared.db.session import AsyncSessionLocal


async def main() -> None:
    filt = sys.argv[1].lower() if len(sys.argv) > 1 else None

    async with AsyncSessionLocal() as db:
        businesses = (
            await db.execute(select(Business).where(Business.is_active == True))  # noqa: E712
        ).scalars().all()

        if filt:
            businesses = [b for b in businesses if filt in b.name.lower()]

        if not businesses:
            print("no matching active businesses found")
            return

        for b in businesses:
            template_caps = verticals.get(b.vertical).get("capabilities", [])
            overrides = (b.settings or {}).get("capability_overrides") or {}
            resolved = verticals.capabilities_for(b)

            print(f"\n=== {b.name} (business_id={b.id}) ===")
            print(f"  vertical (stored):        {b.vertical!r}")
            print(f"  template capabilities:    {template_caps}")
            print(f"  capability_overrides:     {overrides or '(none set)'}")
            print(f"  RESOLVED (what /auth/me sends, what the sidebar filters on):")
            print(f"    {resolved}")

        print("\ndone - nothing was written")


if __name__ == "__main__":
    asyncio.run(main())
