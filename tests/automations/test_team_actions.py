"""
The automation steps that act inside the business (alert the team, hand a chat to
someone, move the pipeline) and the two new triggers, against scripted stand-ins.
"""

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from services.api.routers import post_call_rules as rules  # noqa: E402
from services.api.routers.integrations import _VALID_EVENTS  # noqa: E402
from shared.care import post_call_actions as actions  # noqa: E402
from shared.integrations import web_push  # noqa: E402
from shared.team import routing  # noqa: E402


def _customer(**over):
    base = dict(id=uuid.uuid4(), display_name="Asha", assigned_to_user_id=None, stage=None)
    base.update(over)
    return SimpleNamespace(**base)


class _Rows:
    def __init__(self, ids):
        self._ids = ids

    def scalars(self):
        return SimpleNamespace(all=lambda: self._ids)


class _Db:
    def __init__(self, ids=()):
        self.ids = list(ids)

    async def execute(self, *_a, **_k):
        return _Rows(self.ids)


# ── triggers ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("trigger", ["reply.overdue", "keypad.pressed"])
def test_new_triggers_are_known_and_have_condition_fields(trigger):
    assert trigger in rules._VALID_TRIGGERS
    assert actions.CONDITION_FIELDS[trigger]
    # Automation-only: nothing dispatches them as outbound webhooks.
    assert trigger not in _VALID_EVENTS


def test_a_condition_can_target_the_key_pressed():
    holds, _ = actions._evaluate_one(
        {"field": "digit", "operator": "equals", "value": "1"}, {"digit": "1", "key_name": "sales"},
    )
    assert holds is True


# ── validation ──────────────────────────────────────────────────────────────

def _step(action_type, config):
    return rules.StepIn(action_type=action_type, action_config=config)


@pytest.mark.parametrize("action,config", [
    ("notify_team", {"to": "nobody", "message": "hi"}),
    ("notify_team", {"to": "owner", "message": "  "}),
    ("assign_to_agent", {"to": "someone"}),
    ("set_stage", {"stage": ""}),
    ("set_stage", {"stage": "x" * 61}),
])
def test_incomplete_team_steps_are_refused(action, config):
    with pytest.raises(HTTPException) as err:
        rules._validate_step("message.received", _step(action, config), 0)
    assert err.value.status_code == 422


@pytest.mark.parametrize("action,config", [
    ("notify_team", {"to": "owner", "message": "VIP lead"}),
    ("assign_to_agent", {"to": "round_robin"}),
    ("assign_to_agent", {"to": str(uuid.uuid4())}),
    ("set_stage", {"stage": "Contacted"}),
])
def test_complete_team_steps_are_accepted(action, config):
    rules._validate_step("message.received", _step(action, config), 0)


# ── what they do ────────────────────────────────────────────────────────────

def test_notify_team_pushes_to_the_chosen_people_with_tokens_filled(monkeypatch):
    sent = {}

    async def fake_send(db, *, business_id, user_ids, payload):
        sent.update(user_ids=user_ids, payload=payload)
        return len(user_ids)

    monkeypatch.setattr(web_push, "send_to_users", fake_send)
    owner = uuid.uuid4()
    customer = _customer()

    ok = asyncio.run(actions._notify_team(
        _Db([owner]), uuid.uuid4(), customer, {"to": "owner", "message": "{{name}} pressed {{digit}}"},
        {"digit": "1", "name": "Asha"},
    ))

    assert ok is True and sent["user_ids"] == [owner]
    assert sent["payload"]["url"] == f"/app/inbox/{customer.id}"


def test_notify_team_assigned_goes_to_the_chats_owner_only(monkeypatch):
    sent = {}

    async def fake_send(db, *, business_id, user_ids, payload):
        sent["ids"] = user_ids
        return 1

    monkeypatch.setattr(web_push, "send_to_users", fake_send)
    agent = uuid.uuid4()
    asyncio.run(actions._notify_team(
        _Db([uuid.uuid4()]), uuid.uuid4(), _customer(assigned_to_user_id=agent),
        {"to": "assigned", "message": "waiting"}, {},
    ))
    assert sent["ids"] == [agent]


def test_notify_team_says_so_when_nobody_can_receive_it(monkeypatch):
    async def none_sent(db, *, business_id, user_ids, payload):
        return 0

    monkeypatch.setattr(web_push, "send_to_users", none_sent)
    with pytest.raises(actions.NoRecipient):
        asyncio.run(actions._notify_team(
            _Db([uuid.uuid4()]), uuid.uuid4(), _customer(), {"to": "admins", "message": "x"}, {},
        ))


def test_assign_never_takes_a_chat_someone_already_owns(monkeypatch):
    called = []

    async def fake_assign(*a, **k):
        called.append(1)

    monkeypatch.setattr(routing, "assign", fake_assign)
    with pytest.raises(actions.NoRecipient):
        asyncio.run(actions._assign_to_agent(
            None, uuid.uuid4(), _customer(assigned_to_user_id=uuid.uuid4()), {"to": "round_robin"},
        ))
    assert called == []


def test_assign_round_robin_and_named_member(monkeypatch):
    seen = []

    async def fake_assign(db, *, business_id, customer_id, user_id):
        seen.append(user_id)
        return uuid.uuid4()

    monkeypatch.setattr(routing, "assign", fake_assign)
    member = uuid.uuid4()
    asyncio.run(actions._assign_to_agent(None, uuid.uuid4(), _customer(), {"to": "round_robin"}))
    asyncio.run(actions._assign_to_agent(None, uuid.uuid4(), _customer(), {"to": str(member)}))
    assert seen == [None, member]


def test_assign_reports_when_nobody_is_available(monkeypatch):
    async def nobody(*a, **k):
        return None

    monkeypatch.setattr(routing, "assign", nobody)
    with pytest.raises(actions.NoRecipient):
        asyncio.run(actions._assign_to_agent(None, uuid.uuid4(), _customer(), {"to": "round_robin"}))


def test_set_stage_moves_the_customer():
    customer = _customer()
    assert asyncio.run(actions._set_stage(customer, {"stage": " Contacted "})) is True
    assert customer.stage == "Contacted"
    assert asyncio.run(actions._set_stage(customer, {"stage": " "})) is False
