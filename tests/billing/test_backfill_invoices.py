"""The backfill lists payments that have no invoice, skips small test payments on request, and only writes with --apply."""

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from scripts import backfill_invoices as backfill
from shared.billing import invoices


def pay(purpose, total, day):
    return SimpleNamespace(
        id=uuid.uuid4(), business_id=uuid.uuid4(), purpose=purpose, total_paise=total, txnid=f"KR{day}",
        paid_at=datetime(2026, 10, day, 6, 0, tzinfo=timezone.utc),
    )


class FakeDb:
    def __init__(self, payments):
        self.payments = payments

    async def execute(self, _stmt):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.payments))

    async def get(self, _model, _key):
        return SimpleNamespace(id=uuid.uuid4(), name="Shop", settings={})


def test_listing_writes_nothing(monkeypatch):
    made = []

    async def fake_create(*a, **k):
        made.append(1)

    monkeypatch.setattr(invoices, "create_for_payment", fake_create)
    log = asyncio.run(backfill.backfill(FakeDb([pay("plan", 353882, 10), pay("topup", 102360, 11)])))
    assert made == [] and len(log) == 2 and "invoice" in log[0] and "receipt" in log[1]


def test_apply_makes_one_document_per_payment_and_skips_the_tiny_ones(monkeypatch):
    seen = []

    async def fake_create(db, payment, *, business, period_start, period_end):
        seen.append((payment.purpose, period_start, period_end))
        return SimpleNamespace(number=f"AQX/2627/{len(seen):06d}")

    monkeypatch.setattr(invoices, "create_for_payment", fake_create)
    payments = [pay("plan", 100, 9), pay("plan", 353882, 10), pay("topup", 102360, 11)]
    log = asyncio.run(backfill.backfill(FakeDb(payments), min_paise=5000, apply=True))
    assert [s[0] for s in seen] == ["plan", "topup"]
    assert "SKIPPED" in log[0] and "made AQX/2627/000001" in log[1]
    assert seen[0][2] == datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc) and seen[1][1:] == (None, None)
