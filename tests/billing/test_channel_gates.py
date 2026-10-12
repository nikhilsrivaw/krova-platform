"""A business that has not chosen a plan cannot set up a channel; one that has, can."""

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from services.api.routers import channels, voice_provisioning  # noqa: E402
from shared.billing import guards  # noqa: E402
from shared.config.settings import settings  # noqa: E402

USER = SimpleNamespace(business=uuid.uuid4(), role="owner", id=uuid.uuid4())


class Db:
    """No subaccount exists yet; nothing else is asked."""

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def get(self, model, key, **_):
        return None


@pytest.fixture
def plan(monkeypatch):
    state = {"blocked": True}

    async def blocked(db, business_id):
        return state["blocked"]

    monkeypatch.setattr(guards, "plan_blocked", blocked)
    return state


def _refused(call):
    with pytest.raises(HTTPException) as err:
        asyncio.run(call)
    return err.value


def test_whatsapp_setup_asks_for_a_plan_first(plan, monkeypatch):
    monkeypatch.setattr(settings, "meta_app_id", "1")
    monkeypatch.setattr(settings, "meta_config_id", "2")
    refused = _refused(channels.whatsapp_signup_config(USER, Db()))
    assert refused.status_code == 402 and refused.detail["code"] == "subscription_suspended"
    plan["blocked"] = False
    assert asyncio.run(channels.whatsapp_signup_config(USER, Db())).app_id == "1"


def test_instagram_setup_asks_for_a_plan_first(plan, monkeypatch):
    assert _refused(channels.instagram_connect_url(USER, Db())).status_code == 402
    assert _refused(channels.instagram_fb_connect_url(USER, Db())).status_code == 402
    plan["blocked"] = False
    # past the gate it reaches its own server-config check (not configured in the test environment)
    monkeypatch.setattr(settings, "meta_instagram_app_id", "")
    assert _refused(channels.instagram_connect_url(USER, Db())).status_code == 409


def test_the_voice_account_is_not_created_for_a_business_with_no_plan(plan):
    assert _refused(voice_provisioning.create_subaccount(USER, Db())).status_code == 402
