"""What WhatsApp messages cost the business - shared/channels/whatsapp/usage.py."""

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from shared.channels.whatsapp import usage

BIZ = uuid.uuid4()
WHEN = datetime(2026, 10, 21, 18, 30, tzinfo=timezone.utc)


def _update(status="sent", category="service", pricing_type="regular", errors=None):
    pricing = {"category": category, "type": pricing_type, "billable": pricing_type == "regular"} if category else {}
    return SimpleNamespace(status=status, phone_number_id="pn1", errors=errors or [], occurred_at=WHEN,
                           external_id="wamid.1", raw={"pricing": pricing} if pricing else {})


class _Db:
    def __init__(self, extra=None):
        self.connection = SimpleNamespace(business_id=BIZ, extra=extra or {})
        self.inserts = []

    async def execute(self, statement):
        sql = str(statement)
        if sql.startswith("INSERT"):
            self.inserts.append(statement.compile().params)
            return None
        connection = self.connection
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: connection))


def test_a_charged_reply_is_counted_once_for_the_month():
    db = _Db()
    asyncio.run(usage.record(db, _update()))
    params = db.inserts[0]
    assert params["category"] == "service" and params["pricing_type"] == "regular" and params["messages"] == 1
    assert params["month"] == datetime(2026, 10, 1) and params["business_id"] == BIZ


def test_delivered_and_read_are_not_counted_again():
    db = _Db()
    asyncio.run(usage.record(db, _update(status="delivered")))
    asyncio.run(usage.record(db, _update(status="read")))
    assert db.inserts == []


def test_a_free_message_is_counted_as_free():
    db = _Db()
    asyncio.run(usage.record(db, _update(pricing_type="free_customer_service")))
    assert db.inserts[0]["pricing_type"] == "free_customer_service"


def test_a_payment_failure_is_remembered_and_a_later_charged_send_clears_it():
    db = _Db()
    asyncio.run(usage.record(db, _update(status="failed", category=None,
                                         errors=[{"code": 131042, "title": "Business eligibility payment issue"}])))
    issue = db.connection.extra["payment_issue"]
    assert issue["code"] == 131042 and "payment" in issue["title"]
    asyncio.run(usage.record(db, _update()))
    assert "payment_issue" not in db.connection.extra


def test_other_failures_are_not_a_payment_issue():
    db = _Db()
    asyncio.run(usage.record(db, _update(status="failed", category=None, errors=[{"code": 131047}])))
    assert db.connection.extra == {} and db.inserts == []


def test_a_broken_status_never_raises():
    asyncio.run(usage.record(_Db(), SimpleNamespace(status="sent")))


class _ReadDb:
    def __init__(self, rows, extra):
        self.rows, self.connection = rows, SimpleNamespace(extra=extra)

    async def execute(self, statement):
        rows, connection = self.rows, self.connection
        return SimpleNamespace(all=lambda: rows, scalars=lambda: SimpleNamespace(first=lambda: connection))


def test_this_month_adds_up_charged_and_free():
    rows = [("service", "regular", 40), ("service", "free_customer_service", 1000), ("utility", "regular", 12),
            ("marketing", "free_entry_point", 3)]
    out = asyncio.run(usage.this_month(_ReadDb(rows, {"payment_issue": {"code": 131042}}), BIZ, now=WHEN))
    assert out["by_category"]["service"] == {"charged": 40, "free": 1000}
    assert out["charged_total"] == 52 and out["service_sent"] == 1040 and out["month"] == "2026-10-01"
    assert out["payment_issue"] == {"code": 131042} and out["connected"]
