"""Ask KROVA: the plan is checked, every query compiles for Postgres, customers are found, and the route wires it up."""

import asyncio
import os
import uuid
from datetime import date
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from sqlalchemy.dialects import postgresql  # noqa: E402

from shared.ai import owner_ask  # noqa: E402

TODAY = date(2026, 10, 30)


def test_plan_reads_dates_topics_and_customers():
    plan = owner_ask.read_plan({"period_from": "2026-09-01", "period_to": "2026-09-30",
                                "customers": ["Sharma ji", " "], "topics": ["money", "made_up", "money"]}, TODAY)
    assert (plan.start, plan.end) == (date(2026, 9, 1), date(2026, 9, 30))
    assert plan.customers == ["Sharma ji"] and plan.topics == ["money"]


def test_a_missing_or_broken_plan_is_the_last_30_days_overview():
    plan = owner_ask.read_plan(None, TODAY)
    assert plan.topics == ["overview"] and plan.end == TODAY and (plan.end - plan.start).days == 29
    swapped = owner_ask.read_plan({"period_from": "2026-10-30", "period_to": "2026-10-01", "topics": []}, TODAY)
    assert swapped.start < swapped.end


def test_a_huge_range_is_capped_to_a_year():
    plan = owner_ask.read_plan({"period_from": "2010-01-01", "period_to": "2026-10-30", "topics": ["overview"]}, TODAY)
    assert (plan.end - plan.start).days == owner_ask.MAX_DAYS


def test_the_plan_prompt_names_today_and_every_topic():
    text = owner_ask.plan_prompt(TODAY)
    assert "2026-10-30" in text and all(t in text for t in owner_ask.TOPICS)


class _Result:
    def __init__(self, width=3):
        self.width = width

    def scalar(self):
        return 0

    def scalar_one_or_none(self):
        return None

    def scalar_one(self):
        return 0

    def one(self):
        return (0,) * self.width

    def all(self):
        return []

    def first(self):
        return None

    def scalars(self):
        return self


class _CompilingDb:
    """Compiles every statement for Postgres (a wrong column or operator fails here) and returns nothing."""

    def __init__(self):
        self.statements = 0

    async def execute(self, stmt, *a, **k):
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False})
        self.statements += 1
        return _Result(len(getattr(stmt, "selected_columns", [])) or 1)

    async def get(self, *a, **k):
        return None


def test_every_section_compiles_and_names_its_period(monkeypatch):
    from shared.billing import wallet

    async def no_balance(db, business_id):
        return None

    monkeypatch.setattr(wallet, "balance", no_balance)
    business = SimpleNamespace(id=uuid.uuid4(), name="Sharma Diagnostics", timezone="Asia/Kolkata")
    plan = owner_ask.Plan(start=date(2026, 9, 1), end=date(2026, 9, 30), customers=["Ravi"],
                          topics=list(owner_ask.TOPICS))
    db = _CompilingDb()
    facts = asyncio.run(owner_ask.gather(db, business, plan))
    assert db.statements > 30
    assert "01 Sep 2026 to 30 Sep 2026" in facts
    for heading in ("LEDGER RIGHT NOW", "OVERVIEW", "MONEY", "LEADS", "BOOKINGS", "ORDERS", "CALLS", "TEAM", "VOICE WALLET"):
        assert heading in facts
    assert 'CUSTOMER "Ravi": no customer' in facts
    assert "Not tracked by KROVA" in facts


def test_one_customers_facts_compile():
    db = _CompilingDb()
    customer = SimpleNamespace(
        id=uuid.uuid4(), display_name="Ravi Kumar", assigned_to_user_id=None, first_seen_at=None, created_at=None,
        last_contact_at=None, stage="Won", price_tier=None, deal_value_paise=None, ctwa_headline=None,
    )
    plan = owner_ask.Plan(start=date(2026, 10, 1), end=TODAY)
    window = owner_ask.Window(plan, owner_ask._tz(None))
    lines = asyncio.run(owner_ask.customer_facts(db, uuid.uuid4(), customer, window))
    assert lines[0] == "CUSTOMER Ravi Kumar:" and "Stage: Won." in lines[1]


def test_customers_are_found_by_number_or_name_and_private_ones_never():
    seen = []

    class Db(_CompilingDb):
        def __init__(self, rows):
            super().__init__()
            self.rows = rows

        async def execute(self, stmt, *a, **k):
            seen.append(str(stmt.compile(dialect=postgresql.dialect())))
            rows = self.rows

            class R(_Result):
                def all(self):
                    return rows
            return R()

    public = SimpleNamespace(id=uuid.uuid4(), is_private=False, display_name="Ravi")
    private = SimpleNamespace(id=uuid.uuid4(), is_private=True, display_name="Ravi P")
    found = asyncio.run(owner_ask.find_customers(Db([public, private]), uuid.uuid4(), "+91 98100 12345"))
    assert found == [public] and "customer_identities" in seen[0]
    seen.clear()
    asyncio.run(owner_ask.find_customers(Db([public]), uuid.uuid4(), "Sharma ji"))
    assert "ILIKE" in seen[0].upper() and "ji" not in seen[0].split("ILIKE")[-1].lower().replace("%(", "")


def test_the_route_plans_gathers_then_answers(monkeypatch):
    from services.api.routers import owner
    from shared.ai import client, providers

    calls = []

    async def fake_call(model, request, **kw):
        calls.append(request)
        usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        if request.get("tools"):
            return SimpleNamespace(tool_input={"period_from": "2026-09-01", "period_to": "2026-09-30",
                                               "customers": [], "topics": ["overview"]}, text="", usage=usage, cost_paise=1)
        return SimpleNamespace(tool_input=None, text=" Pichhle mahine 12 naye customer aaye. ", usage=usage, cost_paise=2)

    async def fake_gather(db, business, plan):
        return f"FACTS for {plan.start} {plan.topics}"

    class Db:
        async def get(self, model, key):
            return SimpleNamespace(id=key, name="X", timezone="Asia/Kolkata")

    monkeypatch.setattr(providers, "call", fake_call)
    monkeypatch.setattr(owner_ask, "gather", fake_gather)
    monkeypatch.setattr(client, "_log_usage", lambda *a, **k: None)
    body = owner.AskIn(question="business mein pichhle mahine kya hua?")
    out = asyncio.run(owner.ask(body, SimpleNamespace(business=uuid.uuid4()), Db()))
    assert out.answer == "Pichhle mahine 12 naye customer aaye."
    assert calls[0]["tool_choice"]["name"] == "look_up"
    assert "FACTS for 2026-09-01 ['overview']" in calls[1]["system"]
    assert calls[1]["messages"][-1]["content"] == "business mein pichhle mahine kya hua?"


def test_a_refused_provider_is_a_plain_503(monkeypatch):
    from fastapi import HTTPException

    from services.api.routers import owner
    from shared.ai import providers

    async def refuse(*a, **k):
        raise providers.ProviderRefused("not approved")

    class Db:
        async def get(self, model, key):
            return SimpleNamespace(id=key, name="X", timezone=None)

    monkeypatch.setattr(providers, "call", refuse)
    with pytest.raises(HTTPException) as err:
        asyncio.run(owner.ask(owner.AskIn(question="hi"), SimpleNamespace(business=uuid.uuid4()), Db()))
    assert err.value.status_code == 503


def test_the_answer_is_cleaned_of_markdown_and_accent_marks():
    raw = "**Iss hafte:**  \n* Nayē customer: 1 āē\n### Zarūri\n- नए ग्राहक"
    assert owner_ask.clean_answer(raw) == "Iss hafte:\n- Naye customer: 1 ae\nZaruri\n- नए ग्राहक"


def test_the_rules_ask_for_whatsapp_style_hinglish():
    assert "a-z only" in owner_ask.ANSWER_RULES and "grahak" in owner_ask.ANSWER_RULES
