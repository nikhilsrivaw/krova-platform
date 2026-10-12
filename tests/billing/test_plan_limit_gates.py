"""
The gates themselves: each route that adds one more of something calls the plan check before it writes.
A scripted fake session stands in for the database (there is no local Postgres), answering every
count query with a number the test chooses and every Business lookup with a business on a chosen plan.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.api.routers import campaigns, integrations, knowledge, ledger
from shared.db.models import Business
from shared.team import members


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value

    def first(self):
        return None


class FakeDb:
    def __init__(self, plan, count=0):
        self.business = SimpleNamespace(id=uuid.uuid4(), plan=plan, settings={})
        self.count = count
        self.added = []

    async def get(self, model, key):
        return self.business if model is Business else None

    async def execute(self, *_a, **_k):
        return FakeResult(self.count)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        # What the real database fills in on insert.
        for obj in self.added:
            if getattr(obj, "failure_count", 0) is None:
                obj.failure_count = 0


def user(db):
    return SimpleNamespace(business=db.business.id, id=uuid.uuid4(), role="owner")


def test_starter_cannot_create_an_api_key():
    async def go():
        db = FakeDb("starter", count=0)
        with pytest.raises(HTTPException) as e:
            await integrations.create_api_key(integrations.ApiKeyIn(name="x"), user(db), db)
        assert e.value.status_code == 402 and db.added == []

    asyncio.run(go())


def test_pro_creates_its_second_key_but_not_its_third():
    async def go():
        db = FakeDb("pro", count=1)
        out = await integrations.create_api_key(integrations.ApiKeyIn(name="x"), user(db), db)
        assert out.raw_key and len(db.added) == 1
        db.count = 2
        with pytest.raises(HTTPException) as e:
            await integrations.create_api_key(integrations.ApiKeyIn(name="y"), user(db), db)
        assert e.value.status_code == 402

    asyncio.run(go())


def test_a_business_with_no_sized_plan_is_not_limited():
    async def go():
        db = FakeDb("trial", count=500)
        out = await integrations.create_api_key(integrations.ApiKeyIn(name="x"), user(db), db)
        assert out.raw_key

    asyncio.run(go())


def test_starter_cannot_create_a_webhook():
    async def go():
        db = FakeDb("starter", count=0)
        body = integrations.WebhookIn(target_url="https://example.com/h", event_types=["message.received"])
        with pytest.raises(HTTPException) as e:
            await integrations.create_webhook(body, user(db), db)
        assert e.value.status_code == 402

    asyncio.run(go())


def test_pro_webhook_limit_is_five():
    async def go():
        db = FakeDb("pro", count=4)
        body = integrations.WebhookIn(target_url="https://example.com/h", event_types=["message.received"])
        assert (await integrations.create_webhook(body, user(db), db)).id
        db.count = 5
        with pytest.raises(HTTPException):
            await integrations.create_webhook(body, user(db), db)

    asyncio.run(go())


def test_team_member_over_the_plan_is_refused_with_402():
    async def go():
        db = FakeDb("starter", count=3)  # the owner plus two: the plan's three logins are used
        with pytest.raises(members.TeamError) as e:
            await members.create_member(
                db, business=db.business, actor_id=uuid.uuid4(), actor_role="owner",
                full_name="Asha", handle="asha", role="agent",
            )
        assert e.value.status == 402 and "3 team logins" in str(e.value) and "Pro" in str(e.value)

    asyncio.run(go())


def test_knowledge_over_the_plan_is_refused():
    async def go():
        db = FakeDb("starter", count=5900)  # 5,900 tokens already used of 6,000
        with pytest.raises(HTTPException) as e:
            await knowledge._check_room(db, db.business.id, 200)
        assert e.value.status_code == 402
        await knowledge._check_room(db, db.business.id, 100)  # exactly at the cap is fine

    asyncio.run(go())


def test_import_over_the_plan_is_refused_by_the_whole_file_not_the_batch():
    async def go():
        db = FakeDb("starter")
        rows = [ledger.ContactImportRow(phone="9876543210")]
        with pytest.raises(HTTPException) as e:
            await ledger.import_customers(ledger.ContactImportIn(contacts=rows, total=6000), user(db), db)
        assert e.value.status_code == 402

    asyncio.run(go())


def test_import_with_no_plan_keeps_the_old_5000_ceiling():
    async def go():
        db = FakeDb("trial")
        rows = [ledger.ContactImportRow(phone="9876543210")]
        with pytest.raises(HTTPException) as e:
            await ledger.import_customers(ledger.ContactImportIn(contacts=rows, total=5001), user(db), db)
        assert e.value.status_code == 422

    asyncio.run(go())


def test_usage_card_reports_the_plan_and_none_without_one():
    from services.api.routers import billing

    pro = FakeDb("pro", count=7)
    out = asyncio.run(billing._usage(pro, pro.business.id))
    assert out.messages_limit == 2000 and out.phone_lines_limit == 3 and out.team_limit == 8
    assert out.messages_used == 7 and out.team_used == 7 and out.knowledge_tokens_limit == 10_000

    trial = FakeDb("trial")
    assert asyncio.run(billing._usage(trial, trial.business.id)) is None
