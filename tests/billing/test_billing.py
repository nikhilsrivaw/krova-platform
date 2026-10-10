"""
Billing: the arithmetic, PayU's signing, the read-only rule, and settling a payment
(against scripted stand-ins - no network, no database).
"""

import asyncio
import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.billing import payu, plans, service  # noqa: E402
from shared.config.settings import settings  # noqa: E402

UTC = timezone.utc


# ── the money ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("plan,base,gst,total", [
    ("starter", 299_900, 53_982, 353_882),
    ("pro", 599_900, 107_982, 707_882),
    ("scale", 999_900, 179_982, 1_179_882),
])
def test_plan_prices_with_gst(plan, base, gst, total):
    assert plans.plan_charge(plan) == (base, gst, total)


def test_biggest_plan_fits_under_the_upi_autopay_limit():
    assert plans.plan_charge("scale")[2] <= 15_000_00


def test_topup_adds_gst_then_the_gateway_fee_and_the_wallet_gets_the_full_credit():
    credit, gst, fee, total = plans.topup_charge(1_000_00, 2.36)
    assert credit == 100_000 and gst == 18_000
    assert fee == 2_785                      # 2.36% of 118,000 = 2,784.8, rounded up
    assert total == credit + gst + fee


@pytest.mark.parametrize("bad", [99_00, 50_001_00])
def test_topup_limits(bad):
    with pytest.raises(ValueError):
        plans.topup_charge(bad, 2.36)


def test_calls_are_charged_cost_plus_25_percent_rounded_up():
    assert plans.call_charge(100) == 125
    assert plans.call_charge(101) == 127     # 126.25 -> 127
    assert plans.call_charge(0) == 0


def test_amounts_are_sent_to_payu_in_rupees_with_two_decimals():
    assert plans.rupees(353_882) == "3538.82"
    assert plans.rupees(100) == "1.00"


# ── PayU signing ────────────────────────────────────────────────────────────

@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(settings, "payu_key", "KEY")
    monkeypatch.setattr(settings, "payu_salt", "SALT")
    monkeypatch.setattr(settings, "payu_env", "test")


def _fields():
    return {"txnid": "T1", "amount": "10.00", "productinfo": "P", "firstname": "F", "email": "e@x.in", "udf1": "U1"}


def test_plain_request_hash_matches_payus_formula(keys):
    # udf1=U1, udf2..udf5 empty (4 separators), then the six pipes the formula puts after udf5
    expected = payu.sha512("KEY|T1|10.00|P|F|e@x.in|U1" + "|" * 10 + "SALT")
    assert payu.request_hash(_fields(), None) == expected


def test_subscription_request_hash_puts_si_details_before_the_salt(keys):
    expected = payu.sha512('KEY|T1|10.00|P|F|e@x.in|U1' + "|" * 10 + '{"a":1}|SALT')
    assert payu.request_hash(_fields(), '{"a":1}') == expected


def test_server_calls_are_signed_key_command_var1_salt(keys):
    assert payu.server_hash("verify_payment", "T1") == payu.sha512("KEY|verify_payment|T1|SALT")


def test_checkout_never_sends_the_salt_and_si_flags_the_mandate(keys):
    si = payu.si_details_json(amount_paise=353_882, start=date(2026, 11, 10), end=date(2031, 10, 10))
    fields = payu.checkout_fields(
        txnid="T1", amount_paise=353_882, productinfo="KROVA Starter plan (monthly)", firstname="Asha",
        email="a@b.in", phone="9800000000", return_url="https://x/return", si_details=si,
    )
    assert "SALT" not in json.dumps(fields)
    assert fields["si"] == "1" and fields["api_version"] == "7" and fields["amount"] == "3538.82"
    parsed = json.loads(fields["si_details"])
    assert parsed["billingCycle"] == "MONTHLY" and parsed["billingAmount"] == "3538.82"
    assert fields["surl"] == fields["furl"]


def test_top_up_checkout_is_not_a_mandate(keys):
    fields = payu.checkout_fields(
        txnid="T1", amount_paise=118_000, productinfo="x", firstname="A", email="a@b.in", phone="", return_url="u"
    )
    assert "si" not in fields and "si_details" not in fields


def test_payments_are_refused_until_the_keys_are_set(monkeypatch):
    monkeypatch.setattr(settings, "payu_key", "")
    with pytest.raises(payu.PayUError):
        payu.checkout_fields(txnid="T", amount_paise=100, productinfo="x", firstname="A", email="a@b", phone="", return_url="u")


def test_sandbox_and_live_urls(monkeypatch):
    monkeypatch.setattr(settings, "payu_env", "test")
    assert payu.checkout_url().startswith("https://test.payu.in")
    monkeypatch.setattr(settings, "payu_env", "prod")
    assert payu.checkout_url() == "https://secure.payu.in/_payment"
    assert payu.service_url().startswith("https://info.payu.in")


# ── time and the read-only rule ─────────────────────────────────────────────

def test_add_month_handles_short_months_and_year_end():
    assert service.add_month(datetime(2026, 1, 31, tzinfo=UTC)).date() == date(2026, 2, 28)
    assert service.add_month(datetime(2026, 12, 15, tzinfo=UTC)).date() == date(2027, 1, 15)


def test_transaction_ids_fit_payus_25_character_limit():
    ids = {service.new_txnid() for _ in range(100)}
    assert len(ids) == 100 and all(len(i) <= 25 for i in ids)
    assert len(service.new_txnid("INV")) <= 25


def _sub(status, end_offset_days=10):
    return SimpleNamespace(status=status, current_period_end=datetime.now(UTC) + timedelta(days=end_offset_days))


def test_only_suspended_or_ended_plans_are_read_only():
    assert service.is_blocked(None) is False                       # everyone from before billing
    assert service.is_blocked(_sub("active")) is False
    assert service.is_blocked(_sub("past_due")) is False           # still being retried
    assert service.is_blocked(_sub("suspended")) is True
    assert service.is_blocked(_sub("cancelled", 5)) is False       # paid month still runs
    assert service.is_blocked(_sub("cancelled", -1)) is True


@pytest.mark.parametrize("hh,mm,ok", [
    (2, 0, True), (9, 59, True), (10, 0, False), (12, 59, False), (13, 0, True),
    (16, 59, True), (17, 0, False), (21, 29, False), (21, 30, True), (23, 0, True),
])
def test_debits_avoid_the_hours_upi_autopay_forbids(hh, mm, ok):
    from zoneinfo import ZoneInfo

    ist = datetime(2026, 11, 10, hh, mm, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert service.run_window_ok(ist.astimezone(UTC)) is ok


# ── settling a payment ──────────────────────────────────────────────────────

def _payment(purpose="topup", total=118_000, status="pending", created=None):
    return SimpleNamespace(
        business_id=uuid.uuid4(), purpose=purpose, plan="starter", base_paise=100_000, gst_paise=18_000,
        fee_paise=0, total_paise=total, txnid="T1", status=status, payu_id=None, mode=None, failure=None,
        raw={}, paid_at=None, created_at=created or datetime.now(UTC),
    )


class Db:
    def __init__(self, payment, sub=None, business=None):
        self.payment, self.sub, self.business = payment, sub, business or SimpleNamespace(plan="trial", name="B")
        self.added = []

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.payment)

    async def get(self, model, key, **_):
        return self.sub if model.__name__ == "Subscription" else self.business

    def add(self, obj):
        self.added.append(obj)
        self.sub = obj if type(obj).__name__ == "Subscription" else self.sub


@pytest.fixture
def pay(monkeypatch):
    posted, told = [], []

    async def post(db, **kw):
        posted.append(kw)

    async def tell(db, business_id, title, body):
        told.append(title)

    monkeypatch.setattr(service.wallet, "post", post)
    monkeypatch.setattr(service.wallet, "_tell_owner", tell)

    def verify_with(details):
        async def verify(txnid):
            return details

        monkeypatch.setattr(service.payu, "verify_payment", verify)

    return SimpleNamespace(posted=posted, told=told, verify_with=verify_with)


def test_a_successful_top_up_credits_the_wallet_exactly_once(pay):
    p = _payment()
    pay.verify_with({"status": "success", "mihpayid": "99", "amt": "1180.00", "mode": "UPI"})
    db = Db(p)
    asyncio.run(service.settle_payment(db, "T1"))
    assert p.status == "success" and p.payu_id == "99"
    assert pay.posted == [dict(business_id=p.business_id, amount_paise=100_000, kind="topup", ref="T1", note="Wallet top-up")]
    asyncio.run(service.settle_payment(db, "T1"))     # webhook after the browser return
    assert len(pay.posted) == 1


def test_a_payment_for_the_wrong_amount_is_never_applied(pay):
    p = _payment()
    pay.verify_with({"status": "success", "mihpayid": "99", "amt": "10.00"})
    asyncio.run(service.settle_payment(Db(p), "T1"))
    assert p.status == "failed" and "mismatch" in p.failure and pay.posted == []


def test_unknown_to_payu_stays_pending_until_it_goes_stale(pay):
    fresh = _payment()
    pay.verify_with(None)
    asyncio.run(service.settle_payment(Db(fresh), "T1"))
    assert fresh.status == "pending"
    stale = _payment(created=datetime.now(UTC) - timedelta(days=4))
    asyncio.run(service.settle_payment(Db(stale), "T1"))
    assert stale.status == "failed"


def test_first_plan_payment_creates_the_subscription_and_the_plan(pay):
    p = _payment("plan", total=353_882)
    pay.verify_with({"status": "success", "mihpayid": "555", "amt": "3538.82"})
    db = Db(p)
    asyncio.run(service.settle_payment(db, "T1"))
    sub = db.sub
    assert sub.status == "active" and sub.authpayuid == "555" and sub.plan == "starter"
    assert sub.next_charge_at == sub.current_period_end and db.business.plan == "starter"


def _due_sub(attempts, retry_until=None):
    return SimpleNamespace(
        business_id=uuid.uuid4(), plan="pro", status="active", authpayuid="555", amount_paise=707_882,
        current_period_end=datetime.now(UTC), next_charge_at=datetime.now(UTC), pre_debit_sent_for=None,
        attempts=attempts, retry_until=retry_until, last_failure=None, cancel_at_period_end=False, updated_at=None,
    )


def test_a_failed_renewal_is_retried_51_hours_later(pay):
    sub = _due_sub(attempts=1)
    p = _payment("renewal", total=707_882)
    pay.verify_with({"status": "failure", "mihpayid": "1", "error_Message": "Insufficient funds"})
    asyncio.run(service.settle_payment(Db(p, sub), "T1"))
    assert sub.status == "past_due" and sub.last_failure == "Insufficient funds"
    gap = sub.next_charge_at - datetime.now(UTC)
    assert timedelta(hours=50) < gap <= timedelta(hours=51, minutes=1)
    assert sub.retry_until is not None and pay.told == ["Payment failed - we will try again"]


def test_after_four_attempts_or_a_week_the_plan_is_suspended(pay):
    sub = _due_sub(attempts=4)
    p = _payment("renewal", total=707_882)
    pay.verify_with({"status": "failure", "mihpayid": "1"})
    asyncio.run(service.settle_payment(Db(p, sub), "T1"))
    assert sub.status == "suspended" and sub.next_charge_at is None
    assert pay.told == ["Your KROVA plan is paused"]
    assert service.is_blocked(sub)


def test_a_successful_renewal_extends_a_month_and_clears_the_trouble(pay):
    sub = _due_sub(attempts=2, retry_until=datetime.now(UTC) + timedelta(days=3))
    sub.status = "past_due"
    before = sub.current_period_end
    p = _payment("renewal", total=707_882)
    pay.verify_with({"status": "success", "mihpayid": "2", "amt": "7078.82"})
    asyncio.run(service.settle_payment(Db(p, sub), "T1"))
    assert sub.status == "active" and sub.attempts == 0 and sub.retry_until is None
    assert sub.current_period_end > before + timedelta(days=27)
    assert not service.is_blocked(sub)


# ── who must have a plan ────────────────────────────────────────────────────

def test_only_businesses_created_after_the_cutoff_must_pay(monkeypatch):
    from shared.billing import guards

    old = SimpleNamespace(created_at=datetime(2026, 10, 1, tzinfo=UTC))
    new = SimpleNamespace(created_at=datetime(2026, 11, 20, tzinfo=UTC))
    monkeypatch.setattr(settings, "billing_enforced_from", "")
    assert guards.must_have_plan(new) is False          # switched off by default
    monkeypatch.setattr(settings, "billing_enforced_from", "2026-11-15")
    assert guards.must_have_plan(old) is False and guards.must_have_plan(new) is True
    monkeypatch.setattr(settings, "billing_enforced_from", "not-a-date")
    assert guards.must_have_plan(new) is False
