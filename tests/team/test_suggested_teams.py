"""A business type can suggest its usual teams; for a college they and their words send each question to the right desk."""

import asyncio
import os
import uuid
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest  # noqa: E402

from services.api.routers import routing_rules as api  # noqa: E402
from shared import verticals  # noqa: E402
from shared.db.models import RoutingRule, Team  # noqa: E402
from shared.team import rules  # noqa: E402

SUGGESTED = verticals.get("institution")["suggested_teams"]


def _first_team(text: str) -> str | None:
    """The template's order is the rule order: the first team whose words appear wins."""
    for s in SUGGESTED:
        if rules.matches({"words": s["words"]}, text=text, channel=None, connection_id=None, lead_source=None, tags=set()):
            return s["name"]
    return None


@pytest.mark.parametrize("text, team", [
    ("MBA ki fee kitni hai?", "Admissions"),
    ("BA admission ke liye caste certificate chahiye kya", "Admissions"),
    ("Is this business course recognised?", "Admissions"),
    ("बीए में प्रवेश कब तक है", "Admissions"),
    ("result kab aayega", "Exams & Results"),
    ("प्रवेश पत्र कब मिलेगा", "Exams & Results"),
    ("admit card download nahi ho raha", "Exams & Results"),
    ("fees jama kar di receipt nahi aayi", "Fees & Accounts"),
    ("मुझे फीस वापस चाहिए", "Fees & Accounts"),
    ("hostel fee kitni hai", "Student services"),
    ("tc chahiye mujhe", "Student services"),
    ("scholarship form kab aayega", "Student services"),
    ("I want to watch the campus video", None),
])
def test_each_college_question_reaches_its_desk(text, team):
    assert _first_team(text) == team


def test_every_suggested_word_survives_cleaning():
    for s in SUGGESTED:
        assert rules.clean_conditions({"words": s["words"]})["words"] == s["words"], s["name"]


class _Db:
    def __init__(self, vertical, existing=()):
        self.business = SimpleNamespace(vertical=vertical)
        self.existing = list(existing)
        self.added = []

    async def get(self, model, key):
        return self.business

    async def execute(self, stmt, *a, **k):
        rows = self.existing
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows), scalar=lambda: 0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()


USER = SimpleNamespace(business=uuid.uuid4())


def test_a_college_is_offered_its_teams_and_others_none():
    offered = asyncio.run(api.suggested_teams(current_user=USER, db=_Db("institution")))
    assert [t.name for t in offered] == ["Exams & Results", "Fees & Accounts", "Student services", "Admissions"]
    assert not any(t.exists for t in offered)
    assert asyncio.run(api.suggested_teams(current_user=USER, db=_Db("general"))) == []


def test_making_them_adds_empty_teams_with_their_rules_switched_off():
    db = _Db("institution", existing=["Admissions"])
    made = asyncio.run(api.create_suggested_teams(current_user=USER, db=db))
    assert [t.name for t in made] == ["Exams & Results", "Fees & Accounts", "Student services"]
    teams = [o for o in db.added if isinstance(o, Team)]
    made_rules = [o for o in db.added if isinstance(o, RoutingRule)]
    assert len(teams) == 3 and len(made_rules) == 3
    assert all(not r.active and r.conditions["words"] for r in made_rules)
    assert [r.position for r in made_rules] == [0, 1, 2]
    assert {type(o) for o in db.added} == {Team, RoutingRule}      # nobody is put in a team


def test_nothing_is_made_twice():
    names = [s["name"] for s in SUGGESTED]
    db = _Db("institution", existing=names)
    assert asyncio.run(api.create_suggested_teams(current_user=USER, db=db)) == []
    assert db.added == []
