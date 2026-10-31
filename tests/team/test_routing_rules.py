"""Team routing rules: matching, first rule wins, sharing inside a team, owned chats, and the rule API's checks."""

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

from shared.team import rules  # noqa: E402

LINE = uuid.uuid4()


def m(conditions, text="", channel="whatsapp", number=None, source=None, tags=()):
    return rules.matches(conditions, text=text, channel=channel, connection_id=number, lead_source=source, tags=set(tags))


def test_words_match_anywhere_ignoring_case():
    assert m({"words": ["complaint", "kharab"]}, "Bhai product KHARAB nikla")
    assert not m({"words": ["refund"]}, "price kya hai")


def test_every_set_condition_must_hold_and_empty_ones_are_ignored():
    c = {"words": ["order"], "channels": ["whatsapp"], "numbers": [str(LINE)]}
    assert m(c, "where is my order", number=LINE)
    assert not m(c, "where is my order", channel="instagram", number=LINE)
    assert not m(c, "where is my order", number=uuid.uuid4())
    assert m({}, "anything at all")  # a rule with no conditions catches everything


def test_lead_source_and_tags():
    assert m({"lead_sources": ["IndiaMART"]}, source="indiamart")
    assert not m({"lead_sources": ["indiamart"]}, source=None)
    assert m({"tags": ["VIP"]}, tags=["vip", "jaipur"])
    assert not m({"tags": ["jaipur"]}, tags=["noida"])


def test_conditions_are_cleaned_before_saving():
    out = rules.clean_conditions({"words": [" Refund ", "", "refund"], "channels": ["whatsapp", "fax"], "junk": [1]})
    assert out == {"words": ["Refund", "refund"], "channels": ["whatsapp"]}


class _Db:
    """Returns the business's rules for the rule query and the team for db.get - enough to drive rules.route."""

    def __init__(self, rule_rows, teams):
        self.rule_rows, self.teams = rule_rows, teams

    async def execute(self, stmt, *a, **k):
        stmt.compile(dialect=postgresql.dialect())
        rows = self.rule_rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def get(self, model, key):
        return self.teams.get(key)


def _rule(team_id, conditions, also_if_owned=False):
    return SimpleNamespace(id=uuid.uuid4(), team_id=team_id, conditions=conditions, also_if_owned=also_if_owned)


def _route(monkeypatch, rule_rows, customer, text="", teams=None, assign_people=True, assigned=None):
    sent = []

    async def fake_send(db, *, business_id, customer, team, what, assign=True):
        sent.append(team.id)
        if assigned is not None:
            assigned.append(assign)
        customer.team_id = team.id

    monkeypatch.setattr(rules, "send_to_team", fake_send)
    taken = asyncio.run(rules.route(_Db(rule_rows, teams or {}), business_id=uuid.uuid4(), customer=customer,
                                    text=text, channel="whatsapp", assign_people=assign_people))
    return taken, sent


def _customer(owner=None, team=None):
    return SimpleNamespace(id=uuid.uuid4(), assigned_to_user_id=owner, team_id=team)


def test_the_first_matching_rule_wins(monkeypatch):
    complaints, sales = uuid.uuid4(), uuid.uuid4()
    teams = {complaints: SimpleNamespace(id=complaints), sales: SimpleNamespace(id=sales)}
    taken, sent = _route(monkeypatch, [_rule(complaints, {"words": ["complaint"]}), _rule(sales, {})],
                         _customer(), "I have a complaint", teams)
    assert taken and sent == [complaints]
    taken, sent = _route(monkeypatch, [_rule(complaints, {"words": ["complaint"]}), _rule(sales, {})],
                         _customer(), "price list please", teams)
    assert taken and sent == [sales]


def test_an_owned_chat_moves_only_for_an_also_if_owned_rule(monkeypatch):
    complaints = uuid.uuid4()
    teams = {complaints: SimpleNamespace(id=complaints)}
    taken, sent = _route(monkeypatch, [_rule(complaints, {"words": ["complaint"]})], _customer(owner=uuid.uuid4()),
                         "complaint", teams)
    assert not taken and sent == []
    taken, sent = _route(monkeypatch, [_rule(complaints, {"words": ["complaint"]}, also_if_owned=True)],
                         _customer(owner=uuid.uuid4()), "complaint", teams)
    assert taken and sent == [complaints]


def test_a_chat_already_in_the_team_is_not_moved_again(monkeypatch):
    complaints = uuid.uuid4()
    taken, sent = _route(monkeypatch, [_rule(complaints, {}, also_if_owned=True)],
                         _customer(owner=uuid.uuid4(), team=complaints), "again", {complaints: SimpleNamespace(id=complaints)})
    assert taken and sent == []


def test_no_rule_leaves_business_routing_alone_except_for_a_team_queue(monkeypatch):
    taken, _ = _route(monkeypatch, [], _customer())
    assert taken is False
    taken, _ = _route(monkeypatch, [], _customer(team=uuid.uuid4()))
    assert taken is True  # waiting in a team's queue - not handed out business-wide


def test_inside_a_round_robin_team_the_next_free_member_gets_it_and_is_told(monkeypatch):
    member = SimpleNamespace(user_id=uuid.uuid4(), last_routed_at=None)
    told = []

    async def free(db, team):
        return [member]

    async def notify(db, *, business_id, user_id, title, body, url, kind):
        told.append((user_id, title))

    monkeypatch.setattr(rules, "available_members", free)
    monkeypatch.setattr(rules.conflict, "notify", notify)
    previous = uuid.uuid4()
    team = SimpleNamespace(id=uuid.uuid4(), name="Complaints", sharing="round_robin", lead_user_id=None)
    customer = SimpleNamespace(id=uuid.uuid4(), assigned_to_user_id=previous, assigned_at=None, team_id=None, display_name="Ravi")
    asyncio.run(rules.send_to_team(None, business_id=uuid.uuid4(), customer=customer, team=team))
    assert customer.assigned_to_user_id == member.user_id and customer.team_id == team.id and member.last_routed_at
    assert told[0] == (member.user_id, "New chat for you - Complaints")
    assert told[1][0] == previous and "moved to Complaints" in told[1][1]


def test_nobody_free_waits_in_the_team_and_the_lead_is_told(monkeypatch):
    told = []

    async def free(db, team):
        return []

    async def notify(db, *, business_id, user_id, title, body, url, kind):
        told.append((user_id, title))

    monkeypatch.setattr(rules, "available_members", free)
    monkeypatch.setattr(rules.conflict, "notify", notify)
    lead = uuid.uuid4()
    team = SimpleNamespace(id=uuid.uuid4(), name="Sales", sharing="round_robin", lead_user_id=lead)
    customer = SimpleNamespace(id=uuid.uuid4(), assigned_to_user_id=None, assigned_at=None, team_id=None, display_name=None)
    asyncio.run(rules.send_to_team(None, business_id=uuid.uuid4(), customer=customer, team=team, what="lead"))
    assert customer.assigned_to_user_id is None and customer.team_id == team.id
    assert told == [(lead, "Nobody free in Sales")]


def test_the_rule_api_refuses_a_team_lead_or_member_from_outside_the_team():
    from services.api.routers import routing_rules as api

    staff = {uuid.uuid4()}
    with pytest.raises(HTTPException):
        api._check_team(api.TeamIn(name="X", lead_user_id=uuid.uuid4()), staff)
    with pytest.raises(HTTPException):
        api._check_team(api.TeamIn(name="X", member_ids=[uuid.uuid4()]), staff)
    with pytest.raises(HTTPException):
        api._check_team(api.TeamIn(name="X", sharing="random"), staff)
    api._check_team(api.TeamIn(name="X", member_ids=list(staff)), staff)


def test_the_free_members_query_compiles():
    db = _Db([], {})
    team = SimpleNamespace(id=uuid.uuid4(), business_id=uuid.uuid4())
    assert asyncio.run(rules.available_members(db, team)) == []


# AI first, a person when needed.

def test_the_ai_goes_first_when_it_sends_its_own_replies_or_answers_live():
    assert rules.ai_first("act", "whatsapp") and rules.ai_first("conditional", "instagram")
    assert rules.ai_first("draft", "voice") and rules.ai_first("observe", "web")
    assert not rules.ai_first("draft", "whatsapp") and not rules.ai_first(None, "whatsapp")


def test_ai_first_only_puts_the_chat_in_the_team_unless_the_rule_says_person_now(monkeypatch):
    complaints = uuid.uuid4()
    teams = {complaints: SimpleNamespace(id=complaints)}
    assigned = []
    _route(monkeypatch, [_rule(complaints, {})], _customer(), "hi", teams, assign_people=False, assigned=assigned)
    _route(monkeypatch, [_rule(complaints, {"person_now": True})], _customer(), "hi", teams, assign_people=False,
           assigned=assigned)
    _route(monkeypatch, [_rule(complaints, {})], _customer(), "hi", teams, assign_people=True, assigned=assigned)
    assert assigned == [False, True, True]


def test_person_now_survives_cleaning_and_is_not_a_condition():
    assert rules.clean_conditions({"words": ["refund"], "person_now": True}) == {"words": ["refund"], "person_now": True}
    assert rules.clean_conditions({"person_now": "yes"}) == {}
    assert m({"person_now": True}, "anything")  # the switch alone matches everything, like an empty rule


def test_joining_a_team_without_assigning_tells_nobody_but_the_previous_owner(monkeypatch):
    told = []

    async def notify(db, *, business_id, user_id, title, body, url, kind):
        told.append(user_id)

    monkeypatch.setattr(rules.conflict, "notify", notify)
    previous = uuid.uuid4()
    team = SimpleNamespace(id=uuid.uuid4(), name="Complaints", sharing="round_robin", lead_user_id=uuid.uuid4())
    customer = SimpleNamespace(id=uuid.uuid4(), assigned_to_user_id=previous, assigned_at=None, team_id=None, display_name="Ravi")
    asyncio.run(rules.send_to_team(None, business_id=uuid.uuid4(), customer=customer, team=team, assign=False))
    assert customer.team_id == team.id and customer.assigned_to_user_id is None and told == [previous]


class _HandDb:
    def __init__(self, customer, team=None):
        self.customer, self.team = customer, team

    async def get(self, model, key):
        return self.customer if model is rules.Customer else self.team


def _hand_customer(**kw):
    base = dict(id=uuid.uuid4(), business_id=BIZ, is_private=False, assigned_to_user_id=None, assigned_at=None,
                team_id=None, display_name="Ravi")
    base.update(kw)
    return SimpleNamespace(**base)


BIZ = uuid.uuid4()


def test_hand_to_person_leaves_an_owned_chat_with_its_owner(monkeypatch):
    owner = uuid.uuid4()
    calls = []

    async def no(*a, **k):
        calls.append(1)

    monkeypatch.setattr(rules, "send_to_team", no)
    got = asyncio.run(rules.hand_to_person(_HandDb(_hand_customer(assigned_to_user_id=owner)), business_id=BIZ,
                                           customer_id=uuid.uuid4(), reason="x"))
    assert got == owner and calls == []


def test_hand_to_person_uses_the_chats_team_with_the_reason(monkeypatch):
    member = uuid.uuid4()
    seen = {}

    async def fake_send(db, *, business_id, customer, team, reason=None, **k):
        seen["reason"] = reason
        customer.assigned_to_user_id = member

    monkeypatch.setattr(rules, "send_to_team", fake_send)
    team = SimpleNamespace(id=uuid.uuid4())
    got = asyncio.run(rules.hand_to_person(_HandDb(_hand_customer(team_id=team.id), team), business_id=BIZ,
                                           customer_id=uuid.uuid4(), reason="asked for a person"))
    assert got == member and seen["reason"] == "asked for a person"


def test_hand_to_person_with_no_team_goes_round_robin_to_any_agent(monkeypatch):
    from shared.team import routing

    agent = uuid.uuid4()
    seen = {}

    async def rr(db, *, business_id, customer_id, title, body):
        seen.update(title=title, body=body)
        return agent

    monkeypatch.setattr(routing, "route_new_chat", rr)
    got = asyncio.run(rules.hand_to_person(_HandDb(_hand_customer()), business_id=BIZ, customer_id=uuid.uuid4(),
                                           reason="AI reply waiting for your OK"))
    assert got == agent and seen["body"] == "Ravi - AI reply waiting for your OK"


def test_hand_to_person_ignores_private_chats_and_other_businesses():
    assert asyncio.run(rules.hand_to_person(_HandDb(_hand_customer(is_private=True)), business_id=BIZ,
                                            customer_id=uuid.uuid4(), reason="x")) is None
    assert asyncio.run(rules.hand_to_person(_HandDb(_hand_customer()), business_id=uuid.uuid4(),
                                            customer_id=uuid.uuid4(), reason="x")) is None


def test_chats_no_team_takes_go_round_robin_unless_the_business_assigns_by_hand():
    from shared.team import settings as ts

    assert ts.read(SimpleNamespace(settings={}))["routing"] == "round_robin"
    assert ts.read(SimpleNamespace(settings={"team": {"routing": "manual"}}))["routing"] == "manual"
