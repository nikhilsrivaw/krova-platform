"""
Team v1: adding people, signing them in, and keeping two of them from answering
the same customer.

No Postgres here, so what is tested is the rules (pure functions) and the glue
against a scripted session. The one thing this cannot prove is two requests
racing in a real database: that rests on the single conditional UPDATE in
shared/team/conflict.py, which is why it is one statement.
"""

import asyncio
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from fastapi import HTTPException  # noqa: E402

from shared.auth import service  # noqa: E402
from shared.auth.passwords import MIN_PASSWORD_LENGTH, hash_password  # noqa: E402
from shared.team import conflict, members  # noqa: E402
from shared.team.conflict import Verdict, decide  # noqa: E402

A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


# ── who may add / manage whom ───────────────────────────────────────────────

@pytest.mark.parametrize("actor,new,ok", [
    ("owner", "admin", True), ("owner", "agent", True), ("owner", "owner", False),
    ("admin", "agent", True), ("admin", "admin", False), ("admin", "owner", False),
    ("agent", "agent", False), (None, "agent", False),
])
def test_who_may_add_whom(actor, new, ok):
    assert members.can_add(actor, new) is ok


@pytest.mark.parametrize("actor,target,ok", [
    ("owner", "admin", True), ("owner", "agent", True), ("owner", "owner", False),
    ("admin", "agent", True), ("admin", "admin", False), ("admin", "owner", False),
    ("agent", "agent", False),
])
def test_who_may_reset_or_remove_whom(actor, target, ok):
    assert members.can_manage(actor, target) is ok


def test_generated_passwords_are_long_enough_and_unambiguous():
    for _ in range(50):
        pw = members.generate_password()
        assert len(pw) >= MIN_PASSWORD_LENGTH
        assert re.fullmatch(r"[a-hj-km-np-z2-9]{4}(-[a-hj-km-np-z2-9]{4}){2}", pw)
    assert len({members.generate_password() for _ in range(50)}) == 50
    hash_password(members.generate_password())  # passes the real password policy


@pytest.mark.parametrize("raw,clean", [("Rahul", "rahul"), ("  Rahul Counter ", "rahul.counter"), ("a_b-c.9", "a_b-c.9")])
def test_team_handles_are_cleaned(raw, clean):
    assert members.clean_handle(raw) == clean


@pytest.mark.parametrize("bad", ["", "a", "-rahul", "ra@hul", "rahul!", "x" * 31, "रहul"])
def test_bad_handles_are_refused(bad):
    with pytest.raises(members.TeamError):
        members.clean_handle(bad)


# ── the reply rule ──────────────────────────────────────────────────────────

def test_the_decision_table():
    assert decide(None, A, "agent") is Verdict.claim
    assert decide(A, A, "agent") is Verdict.mine
    assert decide(B, A, "agent") is Verdict.refuse
    assert decide(B, A, "admin") is Verdict.override
    assert decide(B, A, "owner") is Verdict.override
    # A supervisor looking in on an unowned chat does not claim it, so agents stay free to.
    assert decide(None, A, "owner") is Verdict.mine
    assert decide(None, A, "admin") is Verdict.mine


class _Result:
    def __init__(self, value=None, row=None):
        self._value, self._row = value, row

    def scalar_one(self):
        return self._value

    def first(self):
        return self._row


class FakeDb:
    """A scripted session: knows one customer, a member count, and who wins the claim."""

    def __init__(self, customer, members_count=3, claim_wins=True, users=None):
        self.customer = customer
        self.members_count = members_count
        self.claim_wins = claim_wins
        self.users = users or {}
        self.updates = 0

    async def execute(self, stmt):
        text = str(stmt)
        if text.startswith("SELECT count"):
            return _Result(self.members_count)
        if text.startswith("UPDATE customers"):
            self.updates += 1
            if self.claim_wins:
                self.customer.assigned_to_user_id = self._claimant
                return _Result(row=(self.customer.id,))
            return _Result(row=None)
        raise AssertionError(text)

    async def get(self, model, key, **_):
        if model.__name__ == "Customer":
            return self.customer
        return self.users.get(key)

    async def refresh(self, obj):
        return None

    _claimant = None


def _customer(assigned=None):
    return SimpleNamespace(
        id=uuid.uuid4(), business_id=uuid.UUID(int=1), assigned_to_user_id=assigned, assigned_at=None
    )


def _guard(db, customer, actor, role):
    db._claimant = actor
    return asyncio.run(conflict.guard_reply(
        db, business_id=customer.business_id, customer_id=customer.id, actor_id=actor, actor_role=role,
    ))


def test_the_first_agent_to_reply_claims_an_unowned_chat():
    c = _customer()
    db = FakeDb(c)
    assert _guard(db, c, A, "agent") is Verdict.claim
    assert c.assigned_to_user_id == A and db.updates == 1


def test_an_agent_is_refused_in_a_teammates_chat_and_told_whose():
    c = _customer(assigned=B)
    db = FakeDb(c, users={B: SimpleNamespace(full_name="Rahul", username="rahul@shop", email=None)})
    with pytest.raises(conflict.AssignedToOther) as err:
        _guard(db, c, A, "agent")
    assert err.value.assignee_name == "Rahul" and db.updates == 0
    http = conflict.to_http(err.value)
    assert http.status_code == 409
    assert http.detail["code"] == "assigned_to_other" and http.detail["assignee_name"] == "Rahul"


def test_the_agent_who_lost_the_race_is_refused_not_double_assigned():
    # Both read "unassigned"; the other agent's UPDATE landed first, so ours matches no row.
    c = _customer()
    users = {B: SimpleNamespace(full_name="Rahul", username=None, email=None)}

    class Racing(FakeDb):
        async def execute(self, stmt):
            if str(stmt).startswith("UPDATE customers"):
                self.customer.assigned_to_user_id = B  # the winner
                return _Result(row=None)
            return await super().execute(stmt)

    racing = Racing(c, users=users)
    with pytest.raises(conflict.AssignedToOther):
        _guard(racing, c, A, "agent")
    assert c.assigned_to_user_id == B


def test_owner_and_admin_can_step_into_a_teammates_chat_without_taking_it():
    for role in ("owner", "admin"):
        c = _customer(assigned=B)
        assert _guard(FakeDb(c), c, A, role) is Verdict.override
        assert c.assigned_to_user_id == B


def test_a_one_person_business_is_never_blocked_or_claimed():
    c = _customer(assigned=B)
    db = FakeDb(c, members_count=1)
    assert _guard(db, c, A, "agent") is Verdict.mine
    assert db.updates == 0


def test_replying_in_your_own_chat_is_fine():
    c = _customer(assigned=A)
    assert _guard(FakeDb(c), c, A, "agent") is Verdict.mine


# ── team sign-in ────────────────────────────────────────────────────────────

class LoginDb:
    def __init__(self, user, membership=True):
        self.user, self.membership = user, membership
        self.added = []

    async def execute(self, stmt):
        text = str(stmt)
        if "FROM users" in text:
            return SimpleNamespace(scalar_one_or_none=lambda: self.user)
        if "FROM businesses" in text:
            biz = SimpleNamespace(id=uuid.uuid4(), name="Shop", vertical="general")
            return SimpleNamespace(first=lambda: (biz, "agent") if self.membership else None)
        raise AssertionError(text)

    def add(self, obj):
        self.added.append(obj)


def _member(password="correct-horse-1", **over):
    base = dict(
        id=uuid.uuid4(), username="rahul@shop", password_hash=hash_password(password),
        is_active=True, must_change_password=True, failed_logins=0, locked_until=None,
        last_login_at=None, full_name="Rahul", email=None, phone=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def failures(monkeypatch):
    seen = []

    async def record(user_id):
        seen.append(user_id)

    monkeypatch.setattr(service, "_record_failed_login", record)
    return seen


def test_team_login_works_and_flags_the_handed_over_password():
    user = _member()
    session = asyncio.run(service.authenticate_team(" Rahul@Shop ", "correct-horse-1", LoginDb(user)))
    assert session.user is user and user.must_change_password is True
    assert user.last_login_at is not None


def test_wrong_password_and_unknown_id_look_the_same(failures):
    user = _member()
    with pytest.raises(service.InvalidCredentials) as wrong:
        asyncio.run(service.authenticate_team("rahul@shop", "nope-nope-nope", LoginDb(user)))
    with pytest.raises(service.InvalidCredentials) as unknown:
        asyncio.run(service.authenticate_team("nobody@shop", "whatever-123", LoginDb(None)))
    assert str(wrong.value) == str(unknown.value)
    assert failures == [user.id]  # only a real account counts toward the lock


def test_a_locked_account_is_refused_even_with_the_right_password():
    user = _member(locked_until=datetime.now(timezone.utc) + timedelta(minutes=5))
    with pytest.raises(service.AccountLocked) as err:
        asyncio.run(service.authenticate_team("rahul@shop", "correct-horse-1", LoginDb(user)))
    assert 0 < err.value.retry_after_seconds <= 301


def test_an_expired_lock_lets_them_in_and_clears_the_count():
    user = _member(locked_until=datetime.now(timezone.utc) - timedelta(seconds=1), failed_logins=3)
    asyncio.run(service.authenticate_team("rahul@shop", "correct-horse-1", LoginDb(user)))
    assert user.failed_logins == 0 and user.locked_until is None


def test_a_removed_member_cannot_sign_in():
    user = _member()
    with pytest.raises(service.AccountDisabled):
        asyncio.run(service.authenticate_team("rahul@shop", "correct-horse-1", LoginDb(user, membership=False)))
    off = _member(is_active=False)
    with pytest.raises(service.AccountDisabled):
        asyncio.run(service.authenticate_team("rahul@shop", "correct-horse-1", LoginDb(off)))


def test_failed_attempts_lock_after_the_limit(monkeypatch):
    user = _member()
    commits = []

    class Own:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, key, **_):
            return user

        async def commit(self):
            commits.append(1)

    import shared.db.session as session_mod
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", lambda: Own())
    for _ in range(service.MAX_FAILED_LOGINS):
        asyncio.run(service._record_failed_login(user.id))
    assert user.locked_until is not None and user.locked_until > datetime.now(timezone.utc)
    assert user.failed_logins == 0 and len(commits) == service.MAX_FAILED_LOGINS


# ── the app wiring ──────────────────────────────────────────────────────────

def _routes():
    from fastapi.routing import APIRoute
    from services.api.main import app

    return {(m, r.path): r for r in app.routes if isinstance(r, APIRoute) for m in r.methods}


def test_team_routes_exist_with_their_guards():
    from services.api.dependencies import require_owner_or_admin

    routes = _routes()
    p = "/api/v1"
    for key in [
        ("POST", p + "/team/members"), ("PATCH", p + "/team/members/{user_id}"),
        ("DELETE", p + "/team/members/{user_id}"), ("POST", p + "/team/members/{user_id}/reset-password"),
    ]:
        assert require_owner_or_admin in {d.call for d in routes[key].dependant.dependencies}, key
    for key in [("POST", p + "/auth/team-login"), ("POST", p + "/auth/change-password")]:
        assert key in routes
    assert ("POST", p + "/conversations/{customer_id}/take-over") in routes


def test_a_handed_over_password_can_only_be_used_to_replace_it():
    import inspect
    from services.api import dependencies

    src = inspect.getsource(dependencies.get_current_user)
    assert "must_change_password" in src and "password_change_required" in src
    assert dependencies._PASSWORD_PENDING_OK == ("/auth/me", "/auth/change-password", "/auth/logout")


# ── team settings ───────────────────────────────────────────────────────────

def test_team_settings_default_and_round_trip():
    from shared.team import settings as ts

    biz = SimpleNamespace(settings={})
    assert ts.read(biz) == {"auto_assign_on_reply": True, "agent_visibility": "all"}
    ts.write(biz, auto_assign_on_reply=False, agent_visibility="assigned")
    assert ts.read(biz) == {"auto_assign_on_reply": False, "agent_visibility": "assigned"}
    with pytest.raises(ValueError):
        ts.write(biz, auto_assign_on_reply=True, agent_visibility="nobody")
    assert ts.read(SimpleNamespace(settings={"team": {"agent_visibility": "junk"}}))["agent_visibility"] == "all"


def test_unowned_chat_is_not_claimed_when_auto_assign_is_off(monkeypatch):
    async def off(*_a, **_k):
        return False

    monkeypatch.setattr(conflict, "_auto_assign_on", off)
    c = _customer()
    db = FakeDb(c)
    assert _guard(db, c, A, "agent") is Verdict.mine
    assert db.updates == 0 and c.assigned_to_user_id is None


def test_targeted_push_only_reaches_the_named_people(monkeypatch):
    from shared.integrations import web_push

    seen = {}

    async def fake(db, *, business_id, payload, user_ids=None):
        seen["ids"] = user_ids
        return 1

    monkeypatch.setattr(web_push, "send_to_business", fake)
    assert asyncio.run(web_push.send_to_users(None, business_id=uuid.uuid4(), user_ids=[A], payload={})) == 1
    assert seen["ids"] == [A]
    assert asyncio.run(web_push.send_to_users(None, business_id=uuid.uuid4(), user_ids=[], payload={})) == 0
