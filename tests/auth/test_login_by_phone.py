"""
Password sign-in with a phone number. An account made with a phone number has no email but does have the
password chosen at sign-up, so the first box on the login page accepts either.
"""

import asyncio
from types import SimpleNamespace

import pytest

from shared.auth import service
from shared.auth.passwords import hash_password

PASSWORD = "a-long-enough-password"


class FakeDb:
    def __init__(self, user):
        self.user = user
        self.asked = []

    async def execute(self, stmt):
        compiled = stmt.compile()
        self.asked.append((str(compiled), dict(compiled.params)))
        return SimpleNamespace(scalar_one_or_none=lambda: self.user)


@pytest.fixture
def user(monkeypatch):
    u = SimpleNamespace(
        id="u1", email=None, phone="919876543210", password_hash=hash_password(PASSWORD),
        is_active=True, last_login_at=None, locked_until=None, failed_logins=0,
    )

    async def membership(_uid, _db):
        return None

    async def issue(user, business, role, db, record_login=True):
        return SimpleNamespace(user=user)

    async def record_failed(user_id, lock_minutes=service.LOCK_MINUTES):
        # the real one counts in its own database session; here it just notes the call
        u.failures = getattr(u, "failures", 0) + 1
        u.lock_minutes = lock_minutes

    monkeypatch.setattr(service, "_primary_membership", membership)
    monkeypatch.setattr(service, "_issue_session", issue)
    monkeypatch.setattr(service, "_record_failed_login", record_failed)
    return u


@pytest.mark.parametrize("typed", ["9876543210", "+91 98765 43210", "098765 43210", "91-98765-43210"])
def test_a_phone_in_any_common_form_finds_the_account(user, typed):
    db = FakeDb(user)
    out = asyncio.run(service.authenticate(typed, PASSWORD, db))
    assert out.user is user
    sql, params = db.asked[0]
    assert "users.phone" in sql and "919876543210" in params.values()


def test_an_email_still_signs_in_by_email(user):
    user.email, user.phone = "owner@shop.in", None
    db = FakeDb(user)
    asyncio.run(service.authenticate("  Owner@Shop.in ", PASSWORD, db))
    sql, params = db.asked[0]
    assert "users.email" in sql and "owner@shop.in" in params.values()


@pytest.mark.parametrize("typed,password", [("9876543210", "wrong-password-here"), ("not a phone", PASSWORD), ("", PASSWORD)])
def test_wrong_password_or_unusable_identifier_gives_the_same_error(user, typed, password):
    db = FakeDb(user)
    with pytest.raises(service.InvalidCredentials) as e:
        asyncio.run(service.authenticate(typed, password, db))
    assert "incorrect" in str(e.value)


def test_no_such_phone_gives_the_same_error(user):
    with pytest.raises(service.InvalidCredentials):
        asyncio.run(service.authenticate("9123456789", PASSWORD, FakeDb(None)))


def test_a_disabled_account_is_refused(user):
    user.is_active = False
    with pytest.raises(service.AccountDisabled):
        asyncio.run(service.authenticate("9876543210", PASSWORD, FakeDb(user)))


# ── five wrong passwords lock the account for a minute ───────────────────────

def test_a_wrong_password_is_counted_with_a_one_minute_lock(user):
    with pytest.raises(service.InvalidCredentials):
        asyncio.run(service.authenticate("9876543210", "wrong-password-here", FakeDb(user)))
    assert user.failures == 1 and user.lock_minutes == 1


def test_the_real_counter_locks_for_one_minute_on_the_fifth_wrong_password(monkeypatch):
    from datetime import datetime, timezone

    row = SimpleNamespace(failed_logins=4, locked_until=None)

    class Own:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, key, with_for_update=False):
            return row

        async def commit(self):
            pass

    import shared.db.session as session_module

    monkeypatch.setattr(session_module, "AsyncSessionLocal", lambda: Own())
    before = datetime.now(timezone.utc)
    asyncio.run(service._record_failed_login("u1", service.PASSWORD_LOCK_MINUTES))
    assert row.failed_logins == 0
    seconds = (row.locked_until - before).total_seconds()
    assert 55 <= seconds <= 65


def test_the_fourth_wrong_password_does_not_lock_yet(monkeypatch):
    row = SimpleNamespace(failed_logins=3, locked_until=None)

    class Own:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, key, with_for_update=False):
            return row

        async def commit(self):
            pass

    import shared.db.session as session_module

    monkeypatch.setattr(session_module, "AsyncSessionLocal", lambda: Own())
    asyncio.run(service._record_failed_login("u1", 1))
    assert row.failed_logins == 4 and row.locked_until is None


def test_a_locked_account_is_refused_even_with_the_right_password(user):
    from datetime import datetime, timedelta, timezone

    user.locked_until = datetime.now(timezone.utc) + timedelta(seconds=40)
    with pytest.raises(service.AccountLocked) as e:
        asyncio.run(service.authenticate("9876543210", PASSWORD, FakeDb(user)))
    assert 35 <= e.value.retry_after_seconds <= 45 and "Try again in" in str(e.value)
    assert getattr(user, "failures", 0) == 0  # a locked attempt is not counted again


def test_a_finished_lock_lets_the_right_password_in_and_clears_the_count(user):
    from datetime import datetime, timedelta, timezone

    user.locked_until = datetime.now(timezone.utc) - timedelta(seconds=5)
    user.failed_logins = 3
    out = asyncio.run(service.authenticate("9876543210", PASSWORD, FakeDb(user)))
    assert out.user is user and user.failed_logins == 0 and user.locked_until is None


def test_the_login_route_answers_429_with_retry_after_when_locked(user):
    from datetime import datetime, timedelta, timezone

    from fastapi import HTTPException

    from services.api.routers import auth

    user.locked_until = datetime.now(timezone.utc) + timedelta(seconds=30)
    with pytest.raises(HTTPException) as e:
        asyncio.run(auth.login(auth.LoginRequest(email="9876543210", password=PASSWORD), FakeDb(user)))
    assert e.value.status_code == 429 and int(e.value.headers["Retry-After"]) >= 25
