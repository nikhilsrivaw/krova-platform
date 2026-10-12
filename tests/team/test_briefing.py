"""
The pick-up briefing. There is no database here, so the queries are built for real and
compiled to SQL (which catches a wrong column name) and run against an empty stand-in.
"""

import asyncio
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.db.models import Customer  # noqa: E402
from shared.team import briefing  # noqa: E402

BIZ = uuid.uuid4()


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows), first=lambda: (self._rows[0] if self._rows else None))

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _Db:
    """Answers every query with nothing, but compiles each one first."""

    def __init__(self, customer, rows_for=None):
        self.customer = customer
        self.compiled = []
        self.rows_for = rows_for or {}

    async def get(self, model, key):
        return self.customer if model is Customer else None

    async def execute(self, stmt):
        self.compiled.append(str(stmt.compile(dialect=postgresql.dialect())))
        return _Result([])


def _customer(**over):
    base = dict(
        id=uuid.uuid4(), business_id=BIZ, display_name="Asha", is_private=False,
        preferred_language="hi-IN", stage="New", assigned_to_user_id=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_every_query_is_valid_and_an_empty_customer_still_gets_a_briefing():
    customer = _customer()
    db = _Db(customer)
    result = asyncio.run(briefing.build(db, business_id=BIZ, customer_id=customer.id))
    assert result is not None and result.name == "Asha" and result.stage == "New"
    assert result.headline == "Asha"
    assert len(db.compiled) >= 8  # phones, tags, lead, messages, escalation, promises, visit, call, notes


def test_someone_elses_customer_is_not_found():
    customer = _customer(business_id=uuid.uuid4())
    assert asyncio.run(briefing.build(_Db(customer), business_id=BIZ, customer_id=customer.id)) is None


def test_a_private_customer_is_just_a_name_to_a_teammate():
    customer = _customer(is_private=True)
    db = _Db(customer)
    result = asyncio.run(briefing.build(db, business_id=BIZ, customer_id=customer.id, private_ok=False))
    assert result.private and result.summary is None and result.messages == []
    assert db.compiled == []  # nothing else was even read


def test_the_push_line_says_who_what_they_want_and_what_is_pending():
    b = briefing.Briefing(customer_id="1", name="Asha")
    b.lead = {"query": "2BHK in Baner under 80 lakh", "source": "justdial"}
    b.owed_to_us_paise = 500_000
    b.promises = [{"overdue": True}, {"overdue": False}]
    line = briefing.headline(b)
    assert line.startswith("Asha · asked: 2BHK in Baner")
    assert "owes ₹5,000" in line and "1 overdue promise" in line


def test_without_a_lead_the_last_thing_they_said_is_used():
    b = briefing.Briefing(customer_id="1", name="Ravi", last_said="Is the refund done?")
    assert briefing.headline(b) == "Ravi · said: Is the refund done?"


def test_long_text_is_cut_not_dumped():
    assert len(briefing._snip("word " * 200)) <= briefing.SNIPPET
