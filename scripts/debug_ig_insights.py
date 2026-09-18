"""
Temporary diagnostic - which Instagram account-insights metrics actually
work on this connection, live. Delete after use.
"""
import asyncio
import sys

sys.path.insert(0, ".")

import httpx
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
        client = InstagramClient.for_connection(conn)
        url = f"{client._base_url}/{client._ig_user_id}/insights"

        async with httpx.AsyncClient(timeout=25.0) as http:
            bad = {"metric": "xxx_invalid", "period": "day", "access_token": client._token}
            res = await http.get(url, params=bad)
            print("FULL LIST:", res.text)

            names = ["profile_views", "follower_count", "accounts_engaged", "total_interactions", "website_clicks"]
            for metric in names:
                params = {
                    "metric": metric,
                    "period": "day",
                    "metric_type": "total_value",
                    "access_token": client._token,
                }
                res2 = await http.get(url, params=params)
                print(metric, "total_value ->", res2.status_code, res2.text[:300])


asyncio.run(main())
