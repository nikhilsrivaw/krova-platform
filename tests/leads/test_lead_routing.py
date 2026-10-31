"""A new lead from a portal or form gets its owner at once when the team uses round-robin; an owned lead stays put."""

import asyncio
import os
import uuid
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.leads import intake  # noqa: E402
from shared.leads.justdial_parse import ParsedLead  # noqa: E402


class _Db:
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        pass

    def begin_nested(self):
        return _Nested()


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _run(monkeypatch, assigned_to=None, route_raises=False, team_takes=False):
    routed = []
    customer = SimpleNamespace(id=uuid.uuid4(), assigned_to_user_id=assigned_to)

    async def resolve(business_id, kind, value, db, display_name=None):
        return SimpleNamespace(customer=customer)

    async def route(db, *, business_id, customer_id, title, body):
        if route_raises:
            raise RuntimeError("boom")
        routed.append((customer_id, title, body))

    async def rules(*a, **k):
        routed.append("rules")

    async def team_route(db, *, business_id, customer, text, channel, lead_source, what):
        routed.append(("team", lead_source, what))
        return team_takes

    from shared.team import routing, rules as team_rules

    monkeypatch.setattr(team_rules, "route", team_route)
    monkeypatch.setattr(intake.resolver, "resolve", resolve)
    monkeypatch.setattr(routing, "route_new_chat", route)
    monkeypatch.setattr(intake.post_call_actions, "apply_rules", rules)
    business = SimpleNamespace(id=uuid.uuid4())
    parsed = ParsedLead(name="Ravi", phone="9811111111", email=None, query="2BHK in Noida", external_id=None)
    asyncio.run(intake.ingest_parsed(_Db(), business, "justdial", parsed, {}))
    return routed, customer


def test_a_new_lead_is_handed_to_the_next_agent_with_a_lead_notification(monkeypatch):
    routed, customer = _run(monkeypatch)
    assert routed[0] == ("team", "justdial", "lead")
    assert routed[1][0] == customer.id and routed[1][1] == "New lead for you"
    assert "Ravi" in routed[1][2] and "2BHK in Noida" in routed[1][2]
    assert routed[-1] == "rules"  # the lead.received automations still run, after the owner is set


def test_a_lead_from_someone_already_owned_stays_with_them(monkeypatch):
    routed, _ = _run(monkeypatch, assigned_to=uuid.uuid4())
    assert routed == [("team", "justdial", "lead"), "rules"]


def test_a_team_rule_that_takes_the_lead_stops_business_round_robin(monkeypatch):
    routed, _ = _run(monkeypatch, team_takes=True)
    assert routed == [("team", "justdial", "lead"), "rules"]


def test_a_routing_failure_never_loses_the_lead(monkeypatch):
    routed, _ = _run(monkeypatch, route_raises=True)
    assert routed == [("team", "justdial", "lead"), "rules"]
