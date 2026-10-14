"""
Forgot password: prove the email or phone with a code, then choose a new password.
The code itself (shared/auth/otp.py) has its own tests; here it is replaced so the reset flow can be driven alone.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.api.routers import auth
from shared.auth import otp, service
from shared.auth.passwords import hash_password, verify_password

OLD, NEW = "old-password-here", "brand-new-password"


class FakeDb:
    def __init__(self, user):
        self.user, self.asked = user, []

    async def execute(self, stmt):
        compiled = stmt.compile()
        self.asked.append((str(compiled), dict(compiled.params)))
        return SimpleNamespace(scalar_one_or_none=lambda: self.user)


def make_user(**kw):
    base = dict(
        id="u1", email="owner@shop.in", phone="919876543210", password_hash=hash_password(OLD), is_active=True,
        must_change_password=True, failed_logins=4, locked_until="later",
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def calls(monkeypatch):
    log = {"requested": [], "verified": [], "revoked": []}

    async def request(destination, channel, db, *, purpose):
        log["requested"].append((destination, channel, purpose))

    async def verify(destination, channel, code, db):
        log["verified"].append((destination, channel, code))
        if code != "123456":
            raise otp.OtpError("That code is incorrect")

    async def revoke(user_id, db):
        log["revoked"].append(user_id)
        return 1

    monkeypatch.setattr(otp, "request", request)
    monkeypatch.setattr(otp, "verify", verify)
    monkeypatch.setattr(service, "revoke_all_sessions", revoke)
    return log


def ask(db, channel="email", destination="Owner@Shop.in"):
    return asyncio.run(auth.password_reset_request(auth.PasswordResetRequestIn(channel=channel, destination=destination), db))


def confirm(db, code="123456", channel="email", destination="owner@shop.in", password=NEW):
    body = auth.PasswordResetConfirmIn(channel=channel, destination=destination, code=code, new_password=password)
    return asyncio.run(auth.password_reset_confirm(body, db))


def test_a_code_is_sent_to_an_account_that_exists(calls):
    ask(FakeDb(make_user()))
    assert calls["requested"] == [("owner@shop.in", "email", "password_reset")]


def test_a_phone_account_gets_a_call_to_the_normalised_number(calls):
    db = FakeDb(make_user(email=None))
    ask(db, channel="call", destination="+91 98765 43210")
    assert calls["requested"] == [("919876543210", "call", "password_reset")]
    assert "users.phone" in db.asked[0][0]


def test_an_unknown_email_is_told_no_account_found_and_sends_nothing(calls):
    with pytest.raises(HTTPException) as e:
        ask(FakeDb(None))
    assert e.value.status_code == 404 and "No account found" in e.value.detail and "email" in e.value.detail
    assert calls["requested"] == []


def test_an_unknown_phone_number_is_told_the_same_and_no_call_is_placed(calls):
    with pytest.raises(HTTPException) as e:
        ask(FakeDb(None), channel="call", destination="9123456789")
    assert e.value.status_code == 404 and "phone number" in e.value.detail
    assert calls["requested"] == []


def test_a_disabled_account_is_told_so_and_sent_nothing(calls):
    with pytest.raises(HTTPException) as e:
        ask(FakeDb(make_user(is_active=False)))
    assert e.value.status_code == 403
    assert calls["requested"] == []


def test_a_nonsense_phone_number_is_refused(calls):
    with pytest.raises(HTTPException) as e:
        ask(FakeDb(None), channel="call", destination="not a number")
    assert e.value.status_code == 400


def test_the_right_code_sets_the_new_password_and_signs_everyone_out(calls):
    user = make_user()
    confirm(FakeDb(user))
    assert verify_password(NEW, user.password_hash) and not verify_password(OLD, user.password_hash)
    assert user.must_change_password is False and user.failed_logins == 0 and user.locked_until is None
    assert calls["revoked"] == ["u1"]


def test_a_phone_account_can_reset_with_its_phone(calls):
    user = make_user(email=None)
    confirm(FakeDb(user), channel="call", destination="98765 43210")
    assert verify_password(NEW, user.password_hash)
    assert calls["verified"][0][0] == "98765 43210"


def test_a_wrong_code_changes_nothing(calls):
    user = make_user()
    before = user.password_hash
    with pytest.raises(HTTPException) as e:
        confirm(FakeDb(user), code="000000")
    assert e.value.status_code == 400 and user.password_hash == before and calls["revoked"] == []


def test_a_too_short_new_password_is_refused_before_the_code_is_spent(calls):
    with pytest.raises(Exception):
        auth.PasswordResetConfirmIn(channel="email", destination="a@b.co", code="123456", new_password="short")
    assert calls["verified"] == []


def test_a_verified_code_for_a_missing_or_disabled_account_is_refused(calls):
    with pytest.raises(HTTPException) as e:
        confirm(FakeDb(None))
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        confirm(FakeDb(make_user(is_active=False)))
    assert e.value.status_code == 403
