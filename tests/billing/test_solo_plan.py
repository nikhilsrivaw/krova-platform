"""Solo: the entry plan - every channel, the AI, automations and campaigns, one login, and five features left out."""

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.billing import features, limits, plans  # noqa: E402


def test_solo_is_the_cheapest_plan_and_every_plan_fits_upi_autopay():
    assert plans.PLAN_PRICES_PAISE == {"solo": 1_699_00, "starter": 2_999_00, "pro": 7_999_00, "scale": 12_499_00}
    assert plans.PLAN_LABELS["solo"] == "Solo"
    for plan in plans.PLAN_PRICES_PAISE:
        assert plans.plan_charge(plan)[2] <= 15_000_00, plan


def test_solo_is_sized_for_one_person():
    solo, starter = limits.limits_for("solo"), limits.limits_for("starter")
    assert solo.messages == 500 and solo.team_logins == 1 and solo.phone_lines == 1
    assert solo.knowledge_tokens == 4_000 and solo.campaign_audience == 300 and solo.import_rows == 1_000
    assert solo.api_keys == solo.webhooks == solo.lead_sources == 0
    for field in ("messages", "team_logins", "knowledge_tokens", "campaign_audience", "import_rows"):
        assert getattr(solo, field) < getattr(starter, field), field
    with pytest.raises(HTTPException) as e:
        limits.check_count("solo", "team_logins", "team logins", 1)
    assert "Move to Starter" in e.value.detail["message"]


def test_only_solo_leaves_features_out():
    assert features.locked_for("solo") == {"website_chat", "feedback", "analytics", "ask_krova", "team_routing"}
    for plan in ("starter", "pro", "scale", None, "trial"):
        assert features.locked_for(plan) == frozenset()
    solo = SimpleNamespace(plan="solo")
    assert not features.has(solo, features.FEEDBACK) and features.has(SimpleNamespace(plan="starter"), features.FEEDBACK)
    with pytest.raises(HTTPException) as e:
        features.require(solo, features.ASK_KROVA)
    assert e.value.status_code == 402 and "Ask KROVA is not part of the Solo plan" in e.value.detail["message"]


def test_automations_and_campaigns_are_not_locked_on_solo():
    assert not {"automations", "campaigns", "call_campaigns"} & features.locked_for("solo")


def test_the_locked_routers_check_the_plan():
    from services.api.routers import analytics, feedback, owner, routing_rules, web_chat

    for router in (owner.router, routing_rules.router, web_chat.router, feedback.router):
        assert router.dependencies, router.prefix
    locked = {r.path for r in analytics.router.routes if r.dependencies}
    assert locked == {"/analytics/receivables", "/analytics/kept", "/analytics/channels", "/analytics/agent",
                      "/analytics/trust-report", "/analytics/team"}  # the dashboard's /overview stays open


def test_the_dependency_refuses_a_solo_business():
    dep = features.dependency(features.WEBSITE_CHAT).dependency
    user = SimpleNamespace(business=uuid.uuid4())

    class Db:
        def __init__(self, plan):
            self.plan = plan

        async def get(self, model, key):
            return SimpleNamespace(plan=self.plan)

    asyncio.run(dep(user, Db("starter")))
    with pytest.raises(HTTPException):
        asyncio.run(dep(user, Db("solo")))


def test_feedback_never_runs_on_solo_and_the_website_chat_is_hidden():
    from services.api.routers import widget
    from shared.care import feedback

    on = {"feedback": {"enabled": True}}
    assert feedback.read(SimpleNamespace(plan="starter", settings=on))["enabled"]
    assert not feedback.read(SimpleNamespace(plan="solo", settings=on))["enabled"]

    config = SimpleNamespace(business_id=uuid.uuid4())

    class Db:
        async def execute(self, stmt):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: config))

        async def get(self, model, key):
            return SimpleNamespace(plan="solo", is_active=True)

    with pytest.raises(HTTPException) as e:
        asyncio.run(widget._config_and_business("key", Db()))
    assert e.value.status_code == 404
