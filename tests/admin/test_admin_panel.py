"""
The operator panel: only staff who sign in with an emailed-allowlist password may use it, and every query it
runs is valid Postgres SQL. There is no local Postgres, so a fake session compiles each statement against the
Postgres dialect (which catches a malformed query) and answers with empty results.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from services.api.routers import admin
from shared.auth import admin_panel
from shared.config.settings import settings


class _Result:
    def scalar_one(self):
        return 0

    def scalar_one_or_none(self):
        return None

    def all(self):
        return []

    def scalars(self):
        return SimpleNamespace(all=lambda: [], first=lambda: None)


class FakeDb:
    def __init__(self):
        self.sql = []

    async def execute(self, stmt):
        self.sql.append(str(stmt.compile(dialect=postgresql.dialect())))
        return _Result()

    async def get(self, *_a, **_k):
        return None


@pytest.fixture(autouse=True)
def staff(monkeypatch):
    # Fewer iterations keeps the tests quick; the format and the checks are the same.
    monkeypatch.setattr(admin_panel, "ITERATIONS", 1000)
    line = ",".join([admin_panel.format_entry("Boss@Example.com", "boss-password"),
                     admin_panel.format_entry("two@example.com", "second-pass")])
    monkeypatch.setattr(settings, "admin_panel_users", line)
    admin_panel._failures.clear()


def test_the_right_email_and_password_get_a_token_and_it_opens_the_panel():
    out = asyncio.run(admin.login(admin.LoginIn(email=" BOSS@example.com ", password="boss-password")))
    assert out.email == "boss@example.com"
    assert asyncio.run(admin.me(admin._admin(out.token))).email == "boss@example.com"
    assert admin._admin(out.token) == "boss@example.com"


@pytest.mark.parametrize("email,password", [
    ("boss@example.com", "wrong"), ("nobody@example.com", "boss-password"), ("boss@example.com", ""),
])
def test_wrong_credentials_are_refused(email, password):
    with pytest.raises(HTTPException) as e:
        asyncio.run(admin.login(admin.LoginIn(email=email, password=password)))
    assert e.value.status_code == 401


def test_five_wrong_tries_lock_that_email_even_for_the_right_password():
    for _ in range(admin_panel.MAX_FAILURES):
        with pytest.raises(HTTPException):
            asyncio.run(admin.login(admin.LoginIn(email="boss@example.com", password="bad")))
    with pytest.raises(HTTPException) as e:
        asyncio.run(admin.login(admin.LoginIn(email="boss@example.com", password="boss-password")))
    assert e.value.status_code == 429


def test_with_no_users_configured_the_login_is_off(monkeypatch):
    monkeypatch.setattr(settings, "admin_panel_users", "")
    with pytest.raises(HTTPException) as e:
        asyncio.run(admin.login(admin.LoginIn(email="boss@example.com", password="boss-password")))
    assert e.value.status_code == 403


def test_no_token_a_junk_token_or_a_customer_token_cannot_reach_the_panel():
    from shared.auth import tokens

    for bad in (None, "", "junk", tokens.create_connect_state(uuid.uuid4())):
        with pytest.raises(HTTPException) as e:
            admin._admin(bad)
        assert e.value.status_code == 401


def test_a_removed_staff_member_loses_access_at_once(monkeypatch):
    token = admin_panel.login("two@example.com", "second-pass")
    assert admin._admin(token) == "two@example.com"
    monkeypatch.setattr(settings, "admin_panel_users", admin_panel.format_entry("boss@example.com", "boss-password"))
    with pytest.raises(HTTPException):
        admin._admin(token)


def test_overview_and_lists_run_valid_sql():
    db, who = FakeDb(), "boss@example.com"
    out = asyncio.run(admin.overview(who, db))
    assert out.businesses_total == 0 and out.mrr == "₹0.00"
    assert asyncio.run(admin.businesses(who, db, q="acme", limit=10)) == []
    assert asyncio.run(admin.payments(who, db, state="failed", limit=10)) == []
    assert asyncio.run(admin.number_requests(who, db)) == []
    assert any("subscriptions" in s for s in db.sql) and any("number_requests" in s for s in db.sql)


def test_business_detail_runs_valid_sql_and_404s_for_a_stranger():
    from datetime import datetime, timezone

    from shared.db.models import Business

    with pytest.raises(HTTPException) as e:
        asyncio.run(admin.business_detail(uuid.uuid4(), "boss@example.com", FakeDb()))
    assert e.value.status_code == 404

    biz = SimpleNamespace(
        id=uuid.uuid4(), name="Acme", vertical="general", plan="starter", is_active=True, settings={},
        autonomy="draft", timezone="Asia/Kolkata", created_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
    )

    class Db(FakeDb):
        async def get(self, model, key, **_k):
            return biz if model is Business else None

    db = Db()
    out = asyncio.run(admin.business_detail(biz.id, "boss@example.com", db))
    assert out.business.name == "Acme" and out.subscription is None and out.payments == []
    assert any("wallet_entries" in s for s in db.sql)
