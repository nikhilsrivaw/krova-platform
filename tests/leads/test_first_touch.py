"""Speed to lead: when did a person first answer this lead?"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from services.api.routers import leads  # noqa: E402

T0 = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)


def test_the_first_human_contact_after_the_lead_counts():
    touches = [T0 + timedelta(minutes=40), T0 + timedelta(minutes=7), T0 + timedelta(minutes=90)]
    assert leads.first_touch(T0, touches) == T0 + timedelta(minutes=7)


def test_contact_from_before_the_lead_arrived_does_not_count():
    assert leads.first_touch(T0, [T0 - timedelta(days=3)]) is None


def test_nobody_has_answered_yet():
    assert leads.first_touch(T0, []) is None


def test_a_lead_with_no_customer_has_no_touch_data():
    row = SimpleNamespace(
        id=uuid.uuid4(), source="justdial", name="A", phone=None, email=None, query="q", status="no_phone",
        customer_id=None, received_at=T0,
    )
    out = leads._to_out(row)
    assert out.first_touch_at is None and out.assigned_to_name is None


def test_the_touch_queries_are_valid_sql_and_skip_leads_without_a_customer():
    compiled = []

    class Db:
        async def execute(self, stmt):
            compiled.append(str(stmt.compile(dialect=postgresql.dialect())))
            return SimpleNamespace(all=lambda: [])

    cid = uuid.uuid4()
    rows = [
        SimpleNamespace(customer_id=cid, received_at=T0),
        SimpleNamespace(customer_id=None, received_at=T0),
    ]
    result = asyncio.run(leads._human_touches(Db(), uuid.uuid4(), rows))
    assert result == {cid: []} and len(compiled) == 2
    assert asyncio.run(leads._human_touches(Db(), uuid.uuid4(), [SimpleNamespace(customer_id=None, received_at=T0)])) == {}
