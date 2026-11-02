"""The plan's monthly messages: owners told at 80% and 100% on every plan; on Solo the AI stops after a 10% grace."""

import asyncio
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.billing import limits, message_meter  # noqa: E402

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


def _used(monkeypatch, count):
    async def counted(db, business_id):
        return count

    monkeypatch.setattr(limits, "messages_this_month", counted)


def test_only_solo_stops_and_only_after_the_grace(monkeypatch):
    assert message_meter.stop_at("solo") == 550
    for plan in ("starter", "pro", "scale", "trial", None):
        assert message_meter.stop_at(plan) is None
    solo = SimpleNamespace(id=uuid.uuid4(), plan="solo")
    _used(monkeypatch, 549)
    assert not asyncio.run(message_meter.ai_replies_stopped(None, solo))
    _used(monkeypatch, 550)
    assert asyncio.run(message_meter.ai_replies_stopped(None, solo))
    assert not asyncio.run(message_meter.ai_replies_stopped(None, SimpleNamespace(id=uuid.uuid4(), plan="starter")))


class _Db:
    def __init__(self, businesses):
        self.businesses = businesses

    async def execute(self, stmt):
        rows = self.businesses
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


def _business(plan, **settings):
    return SimpleNamespace(id=uuid.uuid4(), plan=plan, settings=settings, is_active=True)


def test_owners_are_told_once_per_threshold_a_month(monkeypatch):
    from shared.billing import wallet

    told = []

    async def tell(db, business_id, title, body):
        told.append(title)

    monkeypatch.setattr(wallet, "_tell_owner", tell)
    starter = _business("starter")
    _used(monkeypatch, 820)
    assert asyncio.run(message_meter.send_notices(_Db([starter]), NOW)) == 1
    assert told == ["820 of 1000 customer messages used"]
    assert asyncio.run(message_meter.send_notices(_Db([starter]), NOW)) == 0  # not twice
    _used(monkeypatch, 1005)
    asyncio.run(message_meter.send_notices(_Db([starter]), NOW))
    assert told[-1] == "1000 customer messages used" and starter.settings["message_notices"]["sent"] == [80, 100]
    _used(monkeypatch, 5000)
    assert asyncio.run(message_meter.send_notices(_Db([starter]), NOW)) == 0  # Starter is never stopped

    solo = _business("solo")
    _used(monkeypatch, 560)
    asyncio.run(message_meter.send_notices(_Db([solo]), NOW))
    assert told[-1] == "Automatic replies paused" and solo.settings["message_notices"]["sent"] == [80, 100, "stopped"]

    next_month = datetime(2026, 12, 2, tzinfo=timezone.utc)
    _used(monkeypatch, 420)
    asyncio.run(message_meter.send_notices(_Db([solo]), next_month))
    assert solo.settings["message_notices"] == {"month": "2026-12", "sent": [80]}


def test_the_billing_page_hears_where_replies_stop(monkeypatch):
    _used(monkeypatch, 600)
    assert asyncio.run(message_meter.usage_for(None, uuid.uuid4(), "solo")) == {"stop_at": 550, "ai_stopped": True}
    assert asyncio.run(message_meter.usage_for(None, uuid.uuid4(), "pro")) == {"stop_at": None, "ai_stopped": False}
