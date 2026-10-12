"""
Calling a customer from the app, and writing the call up.

No phone and no database: Plivo, the guards and the store are scripted, so what is checked is
this code's own decisions - who may call, what is refused and why, what a wrap-up files.
"""

import asyncio
import os
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.channels.voice import human_calls as voice_hooks  # noqa: E402
from shared.channels.voice import plivo_client  # noqa: E402
from shared.db.models import (  # noqa: E402
    Call, Commitment, CommitmentDirection, CommitmentKind, CustomerNote, Direction,
)
from shared.team import conflict, human_calls as svc  # noqa: E402

BIZ, ME, CUST = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return SimpleNamespace(first=lambda: self.value, all=lambda: self.value or [])


class _Db:
    """execute() hands back queued answers in order; get() looks things up by model name."""

    def __init__(self, answers=(), objects=None):
        self.answers = list(answers)
        self.objects = objects or {}
        self.added = []
        self.commits = 0

    async def execute(self, _stmt):
        return _Result(self.answers.pop(0))

    async def get(self, model, key):
        return self.objects.get(model.__name__)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if isinstance(obj, Call) and obj.id is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        self.commits += 1


def _member(phone="919811111111"):
    return SimpleNamespace(phone=phone)


def _customer(**over):
    base = dict(id=CUST, business_id=BIZ, is_private=False, display_name="Asha")
    base.update(over)
    return SimpleNamespace(**base)


def _connection():
    return SimpleNamespace(
        id=uuid.uuid4(), access_token="enc", external_account_id="+912233445566",
        extra={"subaccount_auth_id": "SA1"},
    )


async def _customer_has_a_phone(*a, **k):
    return "919822222222"


@pytest.fixture
def plivo(monkeypatch):
    placed = []

    async def fake_make_call(**kw):
        placed.append(kw)
        return "req"

    async def ok(*a, **k):
        return None

    monkeypatch.setattr(plivo_client, "make_call", fake_make_call)
    monkeypatch.setattr(svc, "decrypt", lambda _: "token")
    monkeypatch.setattr(svc, "require_active_plan", ok)
    monkeypatch.setattr(svc, "require_wallet", ok)
    monkeypatch.setattr(conflict, "guard_reply", ok)
    return placed


def _start(db):
    return asyncio.run(svc.start(db, business_id=BIZ, user_id=ME, role="agent", customer_id=CUST))


def _code(exc: HTTPException) -> str:
    return exc.detail["code"]


# ── starting ────────────────────────────────────────────────────────────────

def test_a_member_with_no_phone_is_told_to_add_one(plivo):
    db = _Db([_member(phone=None)], {"Customer": _customer()})
    with pytest.raises(HTTPException) as err:
        _start(db)
    assert _code(err.value) == "no_member_phone" and plivo == []


def test_a_customer_with_no_number_cannot_be_called(plivo, monkeypatch):
    async def none(*a, **k):
        return None

    monkeypatch.setattr(svc, "_customer_phone", none)
    db = _Db([_member()], {"Customer": _customer()})
    with pytest.raises(HTTPException) as err:
        _start(db)
    assert _code(err.value) == "no_customer_phone"


def test_a_private_chat_is_closed_to_an_agent(plivo):
    db = _Db([_member()], {"Customer": _customer(is_private=True)})
    with pytest.raises(HTTPException) as err:
        _start(db)
    assert _code(err.value) == "private"


def test_no_voice_number_means_no_call(plivo, monkeypatch):
    monkeypatch.setattr(svc, "_customer_phone", _customer_has_a_phone)
    db = _Db([_member(), None], {"Customer": _customer()})
    with pytest.raises(HTTPException) as err:
        _start(db)
    assert _code(err.value) == "no_voice_number"


def test_a_teammates_customer_is_refused_with_their_name(plivo, monkeypatch):
    async def refuse(*a, **k):
        raise conflict.AssignedToOther(uuid.uuid4(), "Ravi", None)

    monkeypatch.setattr(svc, "_customer_phone", _customer_has_a_phone)
    monkeypatch.setattr(conflict, "guard_reply", refuse)
    db = _Db([_member(), _connection(), None], {"Customer": _customer()})
    with pytest.raises(HTTPException) as err:
        _start(db)
    assert err.value.detail["code"] == "assigned_to_other" and "Ravi" in err.value.detail["message"]
    assert plivo == []


def test_the_members_phone_rings_first_and_the_customer_is_dialled_after(plivo, monkeypatch):
    monkeypatch.setattr(svc, "_customer_phone", _customer_has_a_phone)
    db = _Db([_member("919811111111"), _connection(), None], {"Customer": _customer()})
    call = _start(db)

    assert plivo[0]["to_number"] == "919811111111"          # the member, not the customer
    assert plivo[0]["from_number"] == "+912233445566"       # the business number
    assert f"call={call.id}" in plivo[0]["answer_url"] and "/voice/bridge-answer" in plivo[0]["answer_url"]
    assert "/voice/bridge-hangup" in plivo[0]["hangup_url"]
    assert call.handled_by_user_id == ME and call.direction == Direction.outbound
    assert call.external_id.startswith("pending-")


def test_a_second_tap_while_connecting_is_refused(plivo, monkeypatch):
    monkeypatch.setattr(svc, "_customer_phone", _customer_has_a_phone)
    db = _Db([_member(), _connection(), uuid.uuid4()], {"Customer": _customer()})
    with pytest.raises(HTTPException) as err:
        _start(db)
    assert _code(err.value) == "already_calling" and plivo == []


def test_when_plivo_refuses_the_call_is_closed_and_the_member_told(monkeypatch, plivo):
    async def boom(**kw):
        raise plivo_client.PlivoError("nope")

    monkeypatch.setattr(svc, "_customer_phone", _customer_has_a_phone)
    monkeypatch.setattr(plivo_client, "make_call", boom)
    db = _Db([_member(), _connection(), None], {"Customer": _customer()})
    with pytest.raises(HTTPException) as err:
        _start(db)
    assert _code(err.value) == "could_not_place"
    call = next(o for o in db.added if isinstance(o, Call))
    assert call.ended_at is not None and call.wrapped_up_at is not None  # nothing left to write up


# ── states ──────────────────────────────────────────────────────────────────

def test_a_call_moves_from_ringing_to_talking_to_ended():
    now = datetime.now(timezone.utc)
    call = SimpleNamespace(ended_at=None, answered_at=None)
    assert svc.state_of(call) == "ringing_you"
    call.answered_at = now
    assert svc.state_of(call) == "talking"
    call.ended_at = now
    assert svc.state_of(call) == "ended"


# ── writing it up ───────────────────────────────────────────────────────────

def _ended_call(**over):
    base = dict(
        id=uuid.uuid4(), business_id=BIZ, customer_id=CUST, connection_id=uuid.uuid4(),
        ended_at=datetime.now(timezone.utc), duration_seconds=190, wrapped_up_at=None, wrapup_note=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def writeup(monkeypatch):
    sent = []
    msg = SimpleNamespace(id=uuid.uuid4())

    async def fake_ingest(**kw):
        sent.append(kw)
        return SimpleNamespace(message=msg)

    monkeypatch.setattr(svc.ingest, "ingest", fake_ingest)
    monkeypatch.setattr(svc, "_customer_phone", _customer_has_a_phone)
    return SimpleNamespace(sent=sent, msg=msg)


USER = SimpleNamespace(id=ME, full_name="Ravi", username="ravi")


def _wrap(call, note, promises):
    db = _Db(objects={"Customer": _customer()})
    return db, asyncio.run(svc.wrap_up(db, call=call, user=USER, note=note, promises=promises))


def test_a_promise_made_on_the_call_goes_into_the_ledger_with_its_source(writeup):
    call = _ended_call()
    db, filed = _wrap(
        call, "Wants the quote",
        [{"description": "Send the quote", "due_on": date(2026, 10, 14), "kind": "document"}],
    )

    commitment = next(o for o in db.added if isinstance(o, Commitment))
    assert filed == 1
    assert commitment.direction == CommitmentDirection.we_owe and commitment.kind == CommitmentKind.document
    assert commitment.confirmed_by_user_id == ME
    assert commitment.source_message_ids == [writeup.msg.id]
    assert commitment.due_at == datetime(2026, 10, 14, 12, 30, tzinfo=timezone.utc)  # 6pm India time
    assert call.wrapped_up_at is not None and call.wrapup_note == "Wants the quote"


def test_the_note_reaches_the_team_and_the_customers_timeline(writeup):
    db, _ = _wrap(_ended_call(), "Asked for Sunday slot", [])
    assert any(isinstance(o, CustomerNote) and "Asked for Sunday slot" in o.body for o in db.added)
    sent = writeup.sent[0]
    assert sent["direction"] == Direction.outbound and sent["enqueue_analysis"] is False
    assert sent["sent_by_user_id"] == ME and "Call with Ravi (3 min)" in sent["text"]


def test_a_blank_promise_is_dropped_and_extras_are_capped(writeup):
    many = [{"description": f"p{i}"} for i in range(9)] + [{"description": "   "}]
    _, filed = _wrap(_ended_call(), "", many)
    assert filed == svc.MAX_PROMISES


def test_an_unknown_kind_falls_back_to_other(writeup):
    db, _ = _wrap(_ended_call(), "", [{"description": "Call back", "kind": "teleport"}])
    assert next(o for o in db.added if isinstance(o, Commitment)).kind == CommitmentKind.other


def test_a_call_cannot_be_written_up_twice(writeup):
    call = _ended_call(wrapped_up_at=datetime.now(timezone.utc))
    with pytest.raises(HTTPException) as err:
        _wrap(call, "x", [{"description": "again"}])
    assert _code(err.value) == "already_wrapped_up"


def test_a_call_still_in_progress_cannot_be_written_up(writeup):
    with pytest.raises(HTTPException) as err:
        _wrap(_ended_call(ended_at=None), "x", [])
    assert _code(err.value) == "call_in_progress"


def test_writing_up_with_nothing_to_say_just_closes_the_call(writeup):
    _, filed = _wrap(_ended_call(), "", [])
    assert filed == 0 and writeup.sent == []


# ── the webhooks Plivo calls ────────────────────────────────────────────────

class _Req:
    def __init__(self, form):
        self.url = SimpleNamespace(query="call=x")
        self._form = form

    async def form(self):
        return self._form


class _Session:
    def __init__(self, objects):
        self.objects = objects

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, model, key):
        return self.objects.get(model.__name__)

    async def commit(self):
        return None


@pytest.fixture
def hooks(monkeypatch):
    async def build(*a, **k):
        return SimpleNamespace(headline="Asha · asked: 2BHK in Baner")

    monkeypatch.setattr(voice_hooks, "verify", lambda **kw: None)
    monkeypatch.setattr(voice_hooks, "_customer_phone", lambda db, cid: _customer_has_a_phone())
    monkeypatch.setattr(voice_hooks.briefing, "build", build)


def test_when_the_member_picks_up_they_are_told_who_it_is_then_the_customer_is_dialled(hooks, monkeypatch):
    row = SimpleNamespace(
        id=uuid.uuid4(), business_id=BIZ, customer_id=CUST, connection_id=uuid.uuid4(),
        external_id="pending-1", answered_at=None,
    )
    conn = SimpleNamespace(external_account_id="+91 22334 45566")
    session = _Session({"Call": row, "ChannelConnection": conn})
    monkeypatch.setattr(voice_hooks, "AsyncSessionLocal", lambda: session)

    resp = asyncio.run(voice_hooks.bridge_answer(_Req({"CallUUID": "abc-123"}), call=row.id))
    body = resp.body.decode()

    assert "<Speak>Calling Asha asked 2BHK in Baner. Connecting you now.</Speak>" in body
    assert 'callerId="+912233445566"' in body                  # the business number, not the member's
    assert "<Number>+919822222222</Number>" in body            # the customer
    assert row.external_id == "abc-123" and row.answered_at is not None


def test_if_the_customer_does_not_pick_up_the_member_is_told(hooks, monkeypatch):
    row = SimpleNamespace(hangup_cause=None, cost_breakdown={})
    monkeypatch.setattr(voice_hooks, "AsyncSessionLocal", lambda: _Session({"Call": row}))

    resp = asyncio.run(voice_hooks.bridge_dialed(_Req({"DialStatus": "no-answer"}), call=uuid.uuid4()))
    assert "The customer did not pick up" in resp.body.decode()
    assert row.hangup_cause == "no-answer"


def test_a_completed_dial_remembers_the_customer_leg_for_billing(hooks, monkeypatch):
    row = SimpleNamespace(hangup_cause=None, cost_breakdown={"human": True})
    monkeypatch.setattr(voice_hooks, "AsyncSessionLocal", lambda: _Session({"Call": row}))

    asyncio.run(voice_hooks.bridge_dialed(
        _Req({"DialStatus": "completed", "DialBLegUUID": "leg-2"}), call=uuid.uuid4(),
    ))
    assert row.cost_breakdown == {"human": True, "b_leg_uuid": "leg-2"} and row.hangup_cause == "completed"


def test_a_call_the_member_never_answered_has_nothing_to_write_up(hooks, monkeypatch):
    row = SimpleNamespace(ended_at=None, duration_seconds=None, hangup_cause=None, answered_at=None, wrapped_up_at=None)
    monkeypatch.setattr(voice_hooks, "AsyncSessionLocal", lambda: _Session({"Call": row}))
    spawned = []
    monkeypatch.setattr(voice_hooks, "_detach", lambda coro: (spawned.append(1), coro.close()))

    asyncio.run(voice_hooks.bridge_hangup(
        _Req({"Duration": "0", "HangupCauseName": "Normal Clearing"}), call=uuid.uuid4(),
    ))
    assert row.ended_at is not None and row.wrapped_up_at == row.ended_at
    assert spawned == [1]  # the cost step is still scheduled
