"""Disconnecting Instagram destroys the token and marks the connection disconnected, for this business only."""

import asyncio
import uuid
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from services.api.routers import channels
from shared.db.models import ConnectionStatus


class FakeDb:
    def __init__(self, rows):
        self.rows, self.sql = rows, None

    async def execute(self, stmt):
        self.sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.rows))


def test_disconnect_clears_the_credentials_and_only_looks_at_this_business_instagram():
    biz = uuid.uuid4()
    conn = SimpleNamespace(
        access_token="secret", refresh_token="r", status=ConnectionStatus.active, webhook_subscribed=True,
        external_account_id="1789",
    )
    db = FakeDb([conn])
    asyncio.run(channels.disconnect_instagram(SimpleNamespace(business=biz), db))
    assert conn.access_token is None and conn.refresh_token is None
    assert conn.status == ConnectionStatus.disconnected and conn.webhook_subscribed is False
    assert str(biz) in db.sql and "instagram" in db.sql


def test_disconnecting_with_nothing_connected_is_fine():
    asyncio.run(channels.disconnect_instagram(SimpleNamespace(business=uuid.uuid4()), FakeDb([])))
