"""GET /ledger/customers: `q` searches every customer of this business, `customer_id` returns one."""

import asyncio
import uuid
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from services.api.routers import ledger


class CaptureDb:
    def __init__(self):
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))


def _sql(q=None, customer_id=None):
    db = CaptureDb()
    business = uuid.uuid4()
    out = asyncio.run(ledger.list_customers(SimpleNamespace(business=business, role="owner"), db, 50, q, customer_id))
    assert out == []
    sql = str(db.statements[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    return sql, business


def test_search_matches_name_or_identity_within_the_business():
    sql, business = _sql(q="98765 43210")
    assert str(business) in sql.replace("-", "").replace("'", "") or business.hex in sql.replace("-", "")
    assert "ILIKE" in sql.upper() and "customer_identities" in sql
    assert "%9876543210%" in sql  # the digits alone also match a stored +919876543210


def test_one_customer():
    cid = uuid.uuid4()
    sql, _ = _sql(customer_id=cid)
    assert cid.hex in sql.replace("-", "")


def test_no_filters_is_the_plain_recent_list():
    sql, _ = _sql()
    assert "ILIKE" not in sql.upper() and "customer_identities" not in sql
