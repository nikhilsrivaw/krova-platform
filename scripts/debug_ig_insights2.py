"""Temporary diagnostic - does get_account_insights() actually work end to end for the real connection. Delete after use."""
import asyncio
import sys

sys.path.insert(0, ".")

from sqlalchemy import select

from shared.channels.instagram.client import InstagramClient
from shared.db.models import Channel, ChannelConnection, ConnectionStatus
from shared.db.session import AsyncSessionLocal

BIZ = "1863fab8-28ef-405e-88e8-e9f98180659f"


async def main() -> None:
    async with AsyncSessionLocal() as db:
        q = select(ChannelConnection).where(
            ChannelConnection.business_id == BIZ,
            ChannelConnection.channel == Channel.instagram,
            ChannelConnection.status == ConnectionStatus.active,
        )
        result = await db.execute(q)
        conn = result.scalar_one_or_none()
        print("connection found:", conn is not None)
        if conn is None:
            return
        client = InstagramClient.for_connection(conn)
        try:
            insights = await client.get_account_insights(period_days=7)
            print("SUCCESS:", insights)
        except Exception as exc:
            print("FAILED:", type(exc).__name__, str(exc))


asyncio.run(main())
