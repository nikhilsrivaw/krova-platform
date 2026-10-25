"""Each voice number its own job: role, who answers (levels, one by one or together), nobody-answered, and giving it back."""

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.channels.voice import answer, line_profile as lp, missed_calls, number_release  # noqa: E402
from shared.team import call_routing as cr  # noqa: E402

A, B, C, D = (str(uuid.uuid4()) for _ in range(4))
MEMBERS = {A: ("919811111111", "Asha"), B: ("919822222222", "Bilal"), C: ("919833333333", "Chetan")}


def _phone(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) != 12:
        raise ValueError("bad")
    return digits


def _profile(levels, **extra):
    return lp.read({"pickup": {"levels": levels, "max_wait": extra.pop("max_wait", 60)}, **extra})


# ── what is stored ──────────────────────────────────────────────────────────

def test_old_numbers_read_as_no_profile_at_all():
    p = lp.read({"greeting": "hi", "staff_phone_number": "919800000000"})
    assert p["role"] is None and p["pickup"]["levels"] == [] and p["no_answer"] == "callback"


def test_clean_keeps_team_members_and_outside_numbers_in_order():
    out = lp.clean(
        {"role": "sales", "role_note": " Delhi showroom ", "pickup": {"levels": [
            {"mode": "together", "ring_seconds": 99, "people": [{"user_id": A}, {"phone": "+91 98000 00001", "name": "Desk"}]},
            {"people": []},
        ]}, "no_answer": "voicemail", "callback_hours": 0},
        member_ids={A, B}, normalise_phone=_phone,
    )
    assert out["role_note"] == "Delhi showroom"
    assert out["pickup"]["levels"] == [{"mode": "together", "ring_seconds": 45, "people": [
        {"user_id": A}, {"phone": "919800000001", "name": "Desk"}]}]
    assert out["no_answer"] == "voicemail" and out["callback_hours"] == 1


@pytest.mark.parametrize("raw, words", [
    ({"role": "boss"}, "roles"),
    ({"pickup": {"levels": [{"people": [{"user_id": D}]}]}}, "no longer on your team"),
    ({"pickup": {"levels": [{"people": [{"phone": "12"}]}]}}, "not a phone number"),
    ({"pickup": {"levels": [{"people": [{"user_id": A}]}] * 4}}, "Up to 3 levels"),
    ({"no_answer": "hangup"}, "nobody answers"),
])
def test_clean_refuses_what_cannot_work(raw, words):
    with pytest.raises(lp.ProfileError) as err:
        lp.clean(raw, member_ids={A, B}, normalise_phone=_phone)
    assert words in str(err.value)


def test_the_ai_hears_the_role_only_when_one_is_set():
    assert lp.line_note({}, "Sales") is None
    note = lp.line_note({"role": "support", "role_note": "Only for Jaipur orders"}, "Support line")
    assert '"Support line"' in note and "support line" in note and "Only for Jaipur orders" in note


def test_the_line_note_is_shown_to_the_ai_with_the_live_part_of_its_context():
    import dataclasses

    from shared.ai.context import AgentContext

    required = {
        f.name: None for f in dataclasses.fields(AgentContext)
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
    }
    ctx = AgentContext(**{**required, "business_name": "X", "vertical": "general", "known_gaps": [],
                          "escalate_immediately": [], "offerings": {}, "opening_hours": {}, "identities": [],
                          "line_note": "This caller rang the sales line."})
    assert "About this phone line: This caller rang the sales line." in "\n".join(ctx._live_lines())
    assert "sales line" not in "\n".join(ctx._business_lines())


# ── who a call rings ────────────────────────────────────────────────────────

def _numbers(plan):
    return [(t.step, t.number, t.together) for t in plan]


def test_levels_ring_in_order_one_by_one_or_together():
    profile = _profile([
        {"mode": "one_by_one", "ring_seconds": 15, "people": [{"user_id": A}, {"user_id": B}]},
        {"mode": "together", "ring_seconds": 20, "people": [{"user_id": C}, {"phone": "919800000009", "name": "Desk"}]},
    ])
    plan = lp.build_plan(profile, MEMBERS, assigned_user_id=None)
    assert _numbers(plan) == [
        (0, "919811111111", False), (1, "919822222222", False),
        (2, "919833333333", True), (2, "919800000009", True),
    ]


def test_away_members_are_skipped_and_the_customers_own_member_goes_first():
    profile = _profile([{"people": [{"user_id": A}, {"user_id": D}, {"user_id": B}]}])
    plan = lp.build_plan(profile, MEMBERS, assigned_user_id=B)
    assert [t.number for t in plan] == ["919822222222", "919811111111"]


def test_the_longest_wait_stops_adding_steps():
    profile = _profile([{"ring_seconds": 25, "people": [{"user_id": A}, {"user_id": B}, {"user_id": C}]}], max_wait=50)
    assert len(lp.build_plan(profile, MEMBERS, assigned_user_id=None)) == 2


def test_the_plan_survives_the_trip_through_a_url():
    plan = [cr.Target("919811111111", uuid.UUID(A), "a", step=0, ring_seconds=15),
            cr.Target("919822222222", None, "b", step=1, ring_seconds=20, together=True),
            cr.Target("919833333333", uuid.UUID(C), "c", step=1, ring_seconds=20, together=True)]
    steps = cr.decode_plan(cr.encode_plan(plan))
    assert [(s.ring_seconds, s.together, len(s.stops)) for s in steps] == [(15, False, 1), (20, True, 2)]


# ── the endpoint Plivo walks ────────────────────────────────────────────────

class _Req:
    def __init__(self, form):
        self.url = SimpleNamespace(query="q")
        self._form = form

    async def form(self):
        return self._form


@pytest.fixture(autouse=True)
def _no_signature(monkeypatch):
    monkeypatch.setattr(answer, "verify", lambda **kw: None)


def _walk(p, i, status=None, *, no_answer="callback", monkeypatch):
    recorded, promised = [], []

    async def fake_record(call_id, user_id, b_leg=""):
        recorded.append(user_id)

    async def fake_read(connection_id):
        return lp.read({"no_answer": no_answer, "callback_hours": 3})

    async def fake_promise(**kw):
        promised.append(kw)

    class Db:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, key):
            return SimpleNamespace(extra={"no_answer": no_answer, "callback_hours": 3})

    monkeypatch.setattr(answer, "_record_handler", fake_record)
    monkeypatch.setattr(answer, "AsyncSessionLocal", Db)
    monkeypatch.setattr(answer.missed_calls, "promise_callback", fake_promise)
    monkeypatch.setattr(answer.human_calls, "_detach", lambda coro: asyncio.ensure_future(coro))
    form = {} if status is None else {"DialStatus": status, "DialBLegUUID": "leg"}

    async def go():
        resp = await answer.transfer_xml(_Req(form), p=p, i=i, call=uuid.uuid4(), c=uuid.uuid4(), frm="919800000001")
        await asyncio.sleep(0)
        return resp

    return asyncio.run(go()).body.decode(), recorded, promised


PLAN = cr.encode_plan([
    cr.Target("919811111111", uuid.UUID(A), "a", step=0, ring_seconds=15),
    cr.Target("919822222222", uuid.UUID(B), "b", step=1, ring_seconds=20, together=True),
    cr.Target("919833333333", None, "c", step=1, ring_seconds=20, together=True),
])


def test_a_together_step_rings_every_phone_in_one_dial(monkeypatch):
    body, _, _ = _walk(PLAN, 1, "no-answer", monkeypatch=monkeypatch)
    assert "<Number>+919822222222</Number><Number>+919833333333</Number>" in body
    assert 'timeout="20"' in body and "i=2" in body


def test_an_answered_single_step_remembers_who(monkeypatch):
    body, recorded, _ = _walk(PLAN, 1, "completed", monkeypatch=monkeypatch)
    assert "<Hangup" in body and recorded == [uuid.UUID(A)]


def test_nobody_answering_files_a_callback_and_says_when(monkeypatch):
    body, _, promised = _walk(PLAN, 2, "no-answer", monkeypatch=monkeypatch)
    assert "call you back within 3 hours" in body
    assert promised and promised[0]["hours"] == 3 and promised[0]["notify"] == [uuid.UUID(A)]


def test_nobody_answering_can_take_a_voicemail(monkeypatch):
    body, _, promised = _walk(PLAN, 2, "no-answer", no_answer="voicemail", monkeypatch=monkeypatch)
    assert "<Record" in body and "/voice/voicemail?" in body and promised == []


def test_nobody_answering_can_just_apologise(monkeypatch):
    body, _, promised = _walk(PLAN, 2, "busy", no_answer="apology", monkeypatch=monkeypatch)
    assert "nobody is free" in body and "call you back soon" in body and promised == []


def test_the_callback_reads_like_a_task():
    text = missed_calls.describe(who="Ravi", line='the "Sales" line', hours=2, recording_url="https://r", seconds=40)
    assert text.startswith("Call back Ravi") and "40s voicemail" in text and "within 2 hours" in text


# ── giving a number back ───────────────────────────────────────────────────

def test_a_rule_is_tied_to_a_number_only_by_a_number_condition():
    line = uuid.uuid4()
    assert number_release.names_number([{"field": "number", "operator": "equals", "value": str(line)}], line)
    assert number_release.names_number({"field": "number", "value": str(line)}, line)
    assert not number_release.names_number([{"field": "outcome", "value": str(line)}], line)
    assert not number_release.names_number(None, line)


def test_release_moves_campaigns_switches_off_rules_and_hands_over_the_default(monkeypatch):
    from shared.db.models import CallCampaignStatus

    gone = SimpleNamespace(id=uuid.uuid4(), extra={"default_outbound": True}, external_account_id="918000000001")
    heir = SimpleNamespace(id=uuid.uuid4(), extra={"use": "both"}, external_account_id="918000000002")
    sending = SimpleNamespace(connection_id=gone.id, status=CallCampaignStatus.sending)
    draft = SimpleNamespace(connection_id=gone.id, status=CallCampaignStatus.draft)
    rule = SimpleNamespace(is_active=True)

    async def fake_dependents(db, business_id, connection):
        return number_release.Dependents(campaigns=[sending, draft], rules=[rule], is_default=True, others=[heir])

    monkeypatch.setattr(number_release, "dependents", fake_dependents)
    out = asyncio.run(number_release.settle(None, uuid.uuid4(), gone, move_to=None))
    assert sending.status == CallCampaignStatus.paused and sending.connection_id is None and draft.connection_id is None
    assert rule.is_active is False
    assert heir.extra["default_outbound"] is True and gone.extra["default_outbound"] is False
    assert out["campaigns_paused"] == 1 and out["rules_switched_off"] == 1

    sending2 = SimpleNamespace(connection_id=gone.id, status=CallCampaignStatus.sending)

    async def fake_dependents2(db, business_id, connection):
        return number_release.Dependents(campaigns=[sending2], rules=[], is_default=False, others=[heir])

    monkeypatch.setattr(number_release, "dependents", fake_dependents2)
    out = asyncio.run(number_release.settle(None, uuid.uuid4(), gone, move_to=heir))
    assert sending2.connection_id == heir.id and sending2.status == CallCampaignStatus.sending
    assert out["campaigns_moved"] == 1


# ── Live Copilot ─────────────────────────────────────────────────

def _route(**kw):
    base = dict(business_id=uuid.uuid4(), business_name="Sharma Motors", connection_id=uuid.uuid4(),
                staff_phone_number=None, copilot_mode=False, copilot_live_suggestions=False,
                extra={"pickup": {"levels": [{"mode": "together", "people": [{"user_id": A}, {"user_id": B}]}]}})
    base.update(kw)
    return SimpleNamespace(**base)


def _direct(monkeypatch, route):
    class Db:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *_):
            return SimpleNamespace(scalar_one_or_none=lambda: uuid.uuid4())

        async def get(self, *_):
            return SimpleNamespace(id=uuid.uuid4())

    async def fake_plan(db, **kw):
        return [cr.Target("919811111111", uuid.UUID(A), "a", step=0, ring_seconds=18, together=True),
                cr.Target("919822222222", uuid.UUID(B), "b", step=0, ring_seconds=18, together=True)]

    monkeypatch.setattr(answer, "AsyncSessionLocal", Db)
    monkeypatch.setattr(lp, "plan", fake_plan)
    return asyncio.run(answer._direct_to_people(route, "+919800000001"))


def test_live_copilot_rings_the_list_instead_of_one_staff_phone(monkeypatch):
    xml = _direct(monkeypatch, _route(copilot_mode=True, copilot_live_suggestions=True,
                                      extra={"pickup": {"levels": [{"people": [{"user_id": A}]}]}}))
    assert "<Stream" in xml and "copilot-stream" in xml and "<Number>+919811111111</Number>" in xml


def test_a_number_without_a_list_keeps_the_old_behaviour(monkeypatch):
    assert _direct(monkeypatch, _route(copilot_mode=True, extra={})) is None


def test_without_copilot_the_ai_always_answers_first(monkeypatch):
    assert _direct(monkeypatch, _route()) is None
