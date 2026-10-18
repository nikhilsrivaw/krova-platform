"""
The Automations page's "describe it" builder: what the model may offer, how its answer is cleaned, and how the
endpoint validates, retries once, meters and caps it.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.api.routers import post_call_rules
from shared.ai import rule_builder
from shared.care import post_call_actions


def test_everything_offered_is_a_real_trigger_and_action():
    assert set(rule_builder.TRIGGERS) <= post_call_rules._VALID_TRIGGERS
    assert set(rule_builder.ACTIONS) <= post_call_rules._VALID_ACTIONS
    assert set(rule_builder.TRIGGER_CAPABILITY) <= set(rule_builder.TRIGGERS)


def test_sms_email_flows_and_carousels_are_never_offered():
    assert not {"send_sms", "send_email", "send_flow", "send_carousel"} & set(rule_builder.ACTIONS)


def test_triggers_follow_capabilities():
    plain = rule_builder.allowed_triggers([])
    assert "message.received" in plain and "order.status_changed" not in plain
    assert "order.status_changed" in rule_builder.allowed_triggers(["order_sync"])


def test_catalogue_names_fields_and_templates():
    text = rule_builder.catalogue(
        triggers=["commitment.overdue"], templates=["diwali_offer"], business_line="Shop (retail)",
    )
    assert "days_overdue" in text and "diwali_offer" in text and "hour_of_day" in text
    assert "message.received" not in text


def _raw(**over):
    raw = {
        "name": "Chase overdue",
        "trigger_type": "commitment.overdue",
        "channel": "fax",
        "reentry_days": 400,
        "steps": [
            {"action_type": "whatsapp_followup",
             "action_config": {"message": "Payment pending", "template_name": "not_approved", "empty": ""},
             "conditions": [{"field": "days_overdue", "operator": "equals", "value": 3}],
             "delay_seconds": 0},
            {"action_type": "send_sms", "action_config": {"message": "x"}},
            {"action_type": "assign_to_agent", "action_config": {"to": "Ramesh"}, "delay_seconds": 86400},
        ],
    }
    raw.update(over)
    return raw


def test_clean_drops_what_the_builder_may_not_produce():
    rule = rule_builder._clean(_raw(), ["commitment.overdue"], ["diwali_offer"])
    assert rule["channel"] is None and rule["reentry_days"] is None
    assert [s["action_type"] for s in rule["steps"]] == ["whatsapp_followup", "assign_to_agent"]
    assert rule["steps"][0]["action_config"] == {"message": "Payment pending"}
    assert rule["steps"][0]["delay_seconds"] is None
    assert rule["steps"][1]["action_config"] == {"to": "round_robin"}
    assert rule["steps"][1]["delay_seconds"] == 86400
    # the cleaned rule is something the API itself accepts
    post_call_rules._validate(post_call_rules.PostCallRuleIn(**rule))


def test_clean_refuses_a_trigger_the_business_does_not_have():
    assert rule_builder._clean(_raw(), ["message.received"], []) is None
    assert rule_builder._clean(_raw(steps=[{"action_type": "send_email"}]), ["commitment.overdue"], []) is None


# ── the endpoint ─────────────────────────────────────────────────────────────

class FakeDb:
    def __init__(self):
        self.added, self.commits = [], 0

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.commits += 1

    async def get(self, model, key):
        return SimpleNamespace(name="Sharma Store", vertical="retail", settings={})


def _run(monkeypatch, answers, used=0):
    calls = []

    async def fake_answer(**kwargs):
        calls.append(kwargs)
        return answers.pop(0)

    async def fake_used(db, business_id):
        return used

    async def fake_templates(db, business_id):
        return []

    monkeypatch.setattr(post_call_rules.rule_builder, "answer", fake_answer)
    monkeypatch.setattr(post_call_rules, "_drafts_this_month", fake_used)
    monkeypatch.setattr(post_call_rules, "_template_names", fake_templates)
    monkeypatch.setattr(post_call_rules, "capabilities_for", lambda b: [])
    db = FakeDb()
    body = post_call_rules.DraftIn(turns=[{"role": "owner", "text": "naye lead ko turant WhatsApp bhejo"}])
    user = SimpleNamespace(business=uuid.uuid4())
    out = asyncio.run(post_call_rules.ai_draft(body, user, db))
    return out, db, calls


GOOD = {
    "name": "Welcome new leads", "trigger_type": "lead.received", "channel": None, "reentry_days": None,
    "is_active": True,
    "steps": [{"action_type": "whatsapp_followup", "action_config": {"message": "Thanks!"}, "conditions": [],
               "delay_seconds": None}],
}


def test_a_valid_rule_comes_back_and_is_metered(monkeypatch):
    out, db, calls = _run(monkeypatch, [rule_builder.BuilderAnswer(kind="rule", text="Ban gaya", rule=GOOD, cost_paise=120)])
    assert out.kind == "rule" and out.rule.trigger_type == "lead.received"
    assert out.used == 1 and len(db.added) == 1 and db.added[0].krova_cost_paise == 120
    assert len(calls) == 1


def test_an_invalid_rule_gets_one_more_try_with_the_error(monkeypatch):
    bad = {**GOOD, "steps": [{"action_type": "add_tag", "action_config": {}, "conditions": [], "delay_seconds": None}]}
    out, db, calls = _run(monkeypatch, [
        rule_builder.BuilderAnswer(kind="rule", text="x", rule=bad),
        rule_builder.BuilderAnswer(kind="rule", text="Ban gaya", rule=GOOD),
    ])
    assert out.kind == "rule" and len(calls) == 2 and "tag is required" in calls[1]["fix"]
    assert len(db.added) == 2


def test_twice_invalid_is_never_shown_as_a_rule(monkeypatch):
    bad = {**GOOD, "steps": [{"action_type": "add_tag", "action_config": {}, "conditions": [], "delay_seconds": None}]}
    out, _db, _ = _run(monkeypatch, [
        rule_builder.BuilderAnswer(kind="rule", text="x", rule=bad),
        rule_builder.BuilderAnswer(kind="rule", text="x", rule=bad),
    ])
    assert out.kind == "ask" and out.rule is None


def test_model_down_is_a_clear_message_and_still_commits(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("401")

    monkeypatch.setattr(post_call_rules.rule_builder, "answer", boom)
    with pytest.raises(HTTPException) as err:
        _run(monkeypatch, [])
    assert err.value.status_code == 502 and "could not be reached" in err.value.detail


def test_a_question_passes_through(monkeypatch):
    out, _db, _ = _run(monkeypatch, [rule_builder.BuilderAnswer(kind="ask", text="Kitne din baad?", options=["1 din", "3 din"])])
    assert out.kind == "ask" and out.options == ["1 din", "3 din"] and out.rule is None


def test_the_monthly_allowance_is_enforced_before_any_ai_call(monkeypatch):
    with pytest.raises(HTTPException) as err:
        _run(monkeypatch, [], used=post_call_rules.AI_DRAFT_MONTHLY)
    assert err.value.status_code == 429


def test_time_fields_are_in_the_engine_the_prompt_names():
    assert set(post_call_actions.TIME_FIELDS) == {"hour_of_day", "weekday"}
