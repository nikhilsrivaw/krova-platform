"""Plan capacity limits: pure arithmetic, no database."""

import pytest
from fastapi import HTTPException

from shared.billing import limits


def code(exc: HTTPException) -> str:
    return exc.detail["code"]


def test_every_plan_has_limits_and_they_grow():
    s, p, c = (limits.PLAN_LIMITS[k] for k in ("starter", "pro", "scale"))
    for field in ("messages", "phone_lines", "team_logins", "knowledge_tokens", "campaign_audience", "import_rows", "api_keys", "webhooks"):
        assert getattr(s, field) <= getattr(p, field) <= getattr(c, field), field


def test_decided_numbers():
    s = limits.PLAN_LIMITS["starter"]
    assert (s.api_keys, s.webhooks, s.lead_sources) == (0, 0, 1)
    assert limits.PLAN_LIMITS["pro"].api_keys == 2 and limits.PLAN_LIMITS["scale"].api_keys == 10
    assert limits.PLAN_LIMITS["pro"].webhooks == 5 and limits.PLAN_LIMITS["scale"].webhooks == 20
    assert [limits.PLAN_LIMITS[k].import_rows for k in ("starter", "pro", "scale")] == [5000, 20000, 50000]
    assert limits.PLAN_LIMITS["pro"].lead_sources is None and limits.PLAN_LIMITS["scale"].lead_sources is None


def test_unknown_plan_has_no_limits():
    for plan in (None, "trial", ""):
        assert limits.limits_for(plan) is None
        limits.check_count(plan, "api_keys", "API key(s)", 999)
        limits.check_size(plan, "import_rows", "contacts", 10**6)


def test_starter_cannot_make_an_api_key_and_is_pointed_at_pro():
    with pytest.raises(HTTPException) as e:
        limits.check_count("starter", "api_keys", "API key(s)", 0)
    assert e.value.status_code == 402 and code(e.value) == "plan_limit"
    assert "not part of the Starter plan" in e.value.detail["message"] and "Pro" in e.value.detail["message"]


def test_pro_allows_two_keys_not_three():
    limits.check_count("pro", "api_keys", "API key(s)", 1)
    with pytest.raises(HTTPException):
        limits.check_count("pro", "api_keys", "API key(s)", 2)


def test_scale_is_pointed_nowhere_higher():
    with pytest.raises(HTTPException) as e:
        limits.check_count("scale", "webhooks", "outbound webhook(s)", 20)
    assert "Move to" not in e.value.detail["message"]


def test_size_checks():
    limits.check_size("starter", "import_rows", "contacts", 5000)
    with pytest.raises(HTTPException):
        limits.check_size("starter", "import_rows", "contacts", 5001)
    limits.check_size("pro", "campaign_audience", "people", 3000)
    with pytest.raises(HTTPException):
        limits.check_size("pro", "campaign_audience", "people", 3001)


def test_starter_gets_one_lead_source_but_can_reconnect_it():
    none = {}
    limits.check_new_lead_source("starter", none, "justdial_token_hash")
    one = {"justdial_token_hash": "x"}
    limits.check_new_lead_source("starter", one, "justdial_token_hash")  # same source again
    with pytest.raises(HTTPException) as e:
        limits.check_new_lead_source("starter", one, "indiamart_token_hash")
    assert code(e.value) == "plan_limit"


def test_pro_and_scale_connect_every_lead_source():
    many = {k: "x" for k in limits.LEAD_SOURCE_KEYS[:-1]}
    limits.check_new_lead_source("pro", many, limits.LEAD_SOURCE_KEYS[-1])
    limits.check_new_lead_source("scale", many, limits.LEAD_SOURCE_KEYS[-1])


def test_lead_source_keys_match_the_routers():
    from shared.leads import justdial
    from shared.leads.platforms import PLATFORMS

    keys = set(limits.LEAD_SOURCE_KEYS)
    assert justdial.TOKEN_SETTING in keys
    assert {p.token_setting for p in PLATFORMS} <= keys
    assert "indiamart_token_hash" in keys and "email_leads_token_hash" in keys
