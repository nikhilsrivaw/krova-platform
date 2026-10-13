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
        is_active=True, last_login_at=None,
    )

    async def membership(_uid, _db):
        return None

    async def issue(user, business, role, db, record_login=True):
        return SimpleNamespace(user=user)

    monkeypatch.setattr(service, "_primary_membership", membership)
    monkeypatch.setattr(service, "_issue_session", issue)
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
