"""Disconnecting Instagram destroys the token and marks the connection disconnected, for this business only."""

import asyncio
import uuid
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from services.api.routers import channels
from shared.db.models import ConnectionStatus


class FakeDb:
    def __init__(self, rows):
        self.rows, self.sql, self.deleted, self.flushed = rows, None, [], False

    async def execute(self, stmt):
        self.sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.rows))

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        self.flushed = True


def test_disconnect_removes_the_connection_for_this_business_instagram_only():
    biz = uuid.uuid4()
    conn = SimpleNamespace(
        access_token="secret", status=ConnectionStatus.active, webhook_subscribed=True, external_account_id="1789",
    )
    db = FakeDb([conn])
    asyncio.run(channels.disconnect_instagram(SimpleNamespace(business=biz), db))
    assert db.deleted == [conn] and db.flushed
    assert str(biz) in db.sql and "instagram" in db.sql


def test_disconnecting_with_nothing_connected_is_fine():
    db = FakeDb([])
    asyncio.run(channels.disconnect_instagram(SimpleNamespace(business=uuid.uuid4()), db))
    assert db.deleted == []
