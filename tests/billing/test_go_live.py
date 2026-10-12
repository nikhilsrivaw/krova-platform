"""
The renewal path and the gates, checked against what PayU's documentation says.

PayU is scripted (nothing goes to the network) and there is no database, so these prove this code's own
decisions: what it sends PayU, what it does with each answer, and who is switched off when.
"""

import asyncio
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from services.api.routers import billing as billing_router  # noqa: E402
from shared.billing import guards, payu, service  # noqa: E402
from shared.care import post_call_actions as actions  # noqa: E402
from shared.config.settings import settings  # noqa: E402

UTC = timezone.utc


# ── what PayU's verify_payment says ──────────────────────────────────────────

@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(settings, "payu_key", "K")
    monkeypatch.setattr(settings, "payu_salt", "S")
    monkeypatch.setattr(settings, "payu_env", "test")


def _verify(monkeypatch, body):
    async def call(command, var1):
        return body

    monkeypatch.setattr(payu, "_call", call)


def test_a_transaction_payu_has_never_heard_of_is_not_a_result(keys, monkeypatch):
    # PayU answers with a filled-in entry that says "Not Found", not with nothing.
    _verify(monkeypatch, {"status": 0, "transaction_details": {"T1": {"mihpayid": "Not Found", "status": "Not Found"}}})
    assert asyncio.run(payu.verify_payment("T1")) is None


def test_a_real_transaction_is_returned_as_payu_nests_it(keys, monkeypatch):
    entry = {"mihpayid": "403993715525331373", "status": "success", "unmappedstatus": "captured", "amt": "3538.82"}
    _verify(monkeypatch, {"status": 1, "msg": "1 out of 1 Transactions Fetched Successfully", "transaction_details": {"T1": entry}})
    assert asyncio.run(payu.verify_payment("T1")) == entry


def test_pre_debit_carries_the_upi_sequence_number_only_when_given(keys, monkeypatch):
    seen = []

    async def call(command, var1):
        seen.append((command, var1))
        return {"status": 1}

    monkeypatch.setattr(payu, "_call", call)
    base = dict(authpayuid="403", request_id="PD1", debit_date=date(2026, 11, 12), amount_paise=353_882, invoice="INV1")
    asyncio.run(payu.pre_debit(**base))
    asyncio.run(payu.pre_debit(**base, mandate_seq=2))
    assert seen[0][0] == "pre_debit_SI" and "mandateSeqNo" not in seen[0][1]
    assert '"mandateSeqNo":2' in seen[1][1] and '"authpayuid":"403"' in seen[1][1]
    assert '"invoiceDisplayNumber":"INV1"' in seen[1][1] and '"debitDate":"2026-11-12"' in seen[1][1]


# ── the same invoice on the notice and on the charge ─────────────────────────

def test_an_invoice_number_depends_only_on_the_business_and_the_debit_date():
    biz = uuid.uuid4()
    assert service.invoice_for(biz, date(2026, 11, 12)) == service.invoice_for(biz, date(2026, 11, 12))
    assert service.invoice_for(biz, date(2026, 11, 12)) != service.invoice_for(biz, date(2026, 12, 12))
    assert service.invoice_for(biz, date(2026, 11, 12)) != service.invoice_for(uuid.uuid4(), date(2026, 11, 12))
    assert len(service.invoice_for(biz, date(2026, 11, 12))) <= 25


# ── the sweep ────────────────────────────────────────────────────────────────

class Rows:
    def __init__(self, items):
        self.items = list(items)

    def scalars(self):
        return SimpleNamespace(all=lambda: self.items)

    def first(self):
        return self.items[0] if self.items else None


class SweepDb:
    def __init__(self, subs):
        self.subs = subs
        self.added = []

    async def execute(self, stmt):
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        if "FROM subscriptions" in sql:
            return Rows(self.subs)
        if "payments.txnid" in sql:
            return Rows([])
        return Rows([])  # nothing in flight

    async def get(self, model, key, **_):
        return self.subs[0] if self.subs else None

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


def _sub(**over):
    now = datetime.now(UTC)
    base = dict(
        business_id=uuid.uuid4(), plan="starter", status="active", authpayuid="403", amount_paise=353_882,
        current_period_end=now + timedelta(hours=40), next_charge_at=now + timedelta(hours=40),
        pre_debit_sent_for=None, attempts=0, retry_until=None, last_failure=None, cancel_at_period_end=False,
        updated_at=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def payu_stub(keys, monkeypatch):
    calls = SimpleNamespace(pre=[], charge=[], told=[], reply={"status": 1}, charge_reply=None, upi=(False, 2))

    async def pre_debit(**kw):
        calls.pre.append(kw)
        return calls.reply

    async def charge(**kw):
        calls.charge.append(kw)
        return calls.charge_reply or {"status": 1, "details": {kw["txnid"]: {"status": "pending"}}}

    async def mandate(db, sub):
        return calls.upi

    async def tell(db, business_id, title, body):
        calls.told.append(title)

    monkeypatch.setattr(service.payu, "pre_debit", pre_debit)
    monkeypatch.setattr(service.payu, "charge_mandate", charge)
    monkeypatch.setattr(service, "_mandate", mandate)
    monkeypatch.setattr(service.wallet, "_tell_owner", tell)
    monkeypatch.setattr(service, "run_window_ok", lambda now: True)
    return calls


def test_the_notice_goes_out_two_days_ahead_and_names_the_debit_date_and_invoice(payu_stub):
    sub = _sub()
    out = asyncio.run(service.renewal_sweep(SweepDb([sub])))
    assert out["pre_debit"] == 1 and sub.pre_debit_sent_for == sub.next_charge_at
    sent = payu_stub.pre[0]
    assert sent["authpayuid"] == "403" and sent["amount_paise"] == 353_882
    assert sent["debit_date"] == sub.next_charge_at.astimezone(service.IST).date()
    assert sent["invoice"] == service.invoice_for(sub.business_id, sent["debit_date"])
    assert sent["mandate_seq"] is None     # a card mandate has no execution number


def test_a_notice_is_not_sent_before_the_window_opens(payu_stub):
    sub = _sub(next_charge_at=datetime.now(UTC) + timedelta(days=10))
    asyncio.run(service.renewal_sweep(SweepDb([sub])))
    assert payu_stub.pre == []


def test_upi_autopay_gets_its_execution_number(payu_stub):
    payu_stub.upi = (True, 4)
    asyncio.run(service.renewal_sweep(SweepDb([_sub()])))
    assert payu_stub.pre[0]["mandate_seq"] == 4


def test_the_charge_uses_the_same_invoice_the_notice_named(payu_stub):
    sub = _sub(next_charge_at=datetime.now(UTC) - timedelta(minutes=5))
    sub.pre_debit_sent_for = sub.next_charge_at
    debit_on = sub.next_charge_at.astimezone(service.IST).date()
    asyncio.run(service.renewal_sweep(SweepDb([sub])))
    assert payu_stub.charge[0]["invoice"] == service.invoice_for(sub.business_id, debit_on)
    assert payu_stub.charge[0]["authpayuid"] == "403" and payu_stub.charge[0]["amount_paise"] == 353_882


def test_a_notice_payu_already_has_counts_as_sent(payu_stub):
    payu_stub.reply = {"status": "E9254", "msg": "pre-debit already sent for this sequence"}
    sub = _sub()
    asyncio.run(service.renewal_sweep(SweepDb([sub])))
    assert sub.pre_debit_sent_for == sub.next_charge_at and payu_stub.told == []


def test_a_refused_notice_is_not_sent_and_the_owner_is_told_once(payu_stub):
    payu_stub.reply = {"status": 0, "msg": "Mandate is not active"}
    sub = _sub()
    asyncio.run(service.renewal_sweep(SweepDb([sub])))
    asyncio.run(service.renewal_sweep(SweepDb([sub])))
    assert sub.pre_debit_sent_for is None and sub.last_failure == "Mandate is not active"
    assert payu_stub.told == ["Your payment method needs attention"]     # once, not every 30 minutes


@pytest.mark.parametrize("status,reply,failed", [
    ("captured", {"status": 1, "details": {"T": {"status": "captured"}}}, False),
    ("pending", {"status": 1, "details": {"T": {"status": "pending"}}}, False),
    ("failed", {"status": 1, "details": {"T": {"status": "failed"}}}, True),
    ("invalid hash", {"status": 0, "msg": "Invalid Hash."}, True),
])
def test_a_charge_is_only_called_failed_when_payu_says_so(payu_stub, monkeypatch, status, reply, failed):
    sub = _sub(next_charge_at=datetime.now(UTC) - timedelta(minutes=5))
    sub.pre_debit_sent_for = sub.next_charge_at
    db = SweepDb([sub])

    def with_txn(**kw):
        txn = kw["txnid"]
        payu_stub.charge.append(kw)
        body = {k: ({txn: v[next(iter(v))]} if k == "details" else v) for k, v in reply.items()}
        return body

    async def charge(**kw):
        return with_txn(**kw)

    monkeypatch.setattr(service.payu, "charge_mandate", charge)
    asyncio.run(service.renewal_sweep(db))
    payment = db.added[0]
    assert (payment.status == "failed") is failed


def test_a_plan_unpaid_a_week_after_its_month_ended_is_paused_whatever_the_reason(payu_stub):
    sub = _sub(current_period_end=datetime.now(UTC) - timedelta(days=8), next_charge_at=None)
    out = asyncio.run(service.renewal_sweep(SweepDb([sub])))
    assert sub.status == "suspended" and out["suspended"] == 1
    assert payu_stub.told == ["Your KROVA plan is paused"] and service.is_blocked(sub)


def test_the_backstop_leaves_alone_plans_still_inside_the_week_and_cancelled_ones(payu_stub):
    inside = _sub(current_period_end=datetime.now(UTC) - timedelta(days=3), next_charge_at=None)
    cancelled = _sub(current_period_end=datetime.now(UTC) - timedelta(days=30), cancel_at_period_end=True)
    asyncio.run(service.renewal_sweep(SweepDb([inside, cancelled])))
    assert inside.status == "active"
    assert cancelled.status == "cancelled"      # ends cleanly, is not "suspended"


# ── settling: how long we wait ───────────────────────────────────────────────

class OnePayment:
    def __init__(self, payment):
        self.payment = payment

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.payment)

    async def get(self, model, key, **_):
        return None


def _payment(purpose, age):
    return SimpleNamespace(
        business_id=uuid.uuid4(), purpose=purpose, plan="starter", total_paise=353_882, base_paise=299_900,
        gst_paise=53_982, txnid="T1", status="pending", payu_id=None, mode=None, failure=None, raw={},
        paid_at=None, created_at=datetime.now(UTC) - age,
    )


def _payu_says(monkeypatch, details):
    async def verify(txnid):
        return details

    monkeypatch.setattr(service.payu, "verify_payment", verify)


def test_a_renewal_payu_never_heard_of_is_judged_in_hours_but_a_checkout_gets_days(monkeypatch):
    _payu_says(monkeypatch, None)
    renewal, topup = _payment("renewal", timedelta(hours=7)), _payment("topup", timedelta(hours=7))
    asyncio.run(service.settle_payment(OnePayment(renewal), "T1"))
    asyncio.run(service.settle_payment(OnePayment(topup), "T1"))
    assert renewal.status == "failed" and topup.status == "pending"


def test_a_payment_the_bank_never_confirms_is_eventually_failed(monkeypatch):
    _payu_says(monkeypatch, {"status": "pending", "mihpayid": "9"})
    fresh, old = _payment("topup", timedelta(hours=2)), _payment("topup", timedelta(days=4))
    asyncio.run(service.settle_payment(OnePayment(fresh), "T1"))
    asyncio.run(service.settle_payment(OnePayment(old), "T1"))
    assert fresh.status == "pending" and old.status == "failed" and "never confirmed" in old.failure


# ── the gates ────────────────────────────────────────────────────────────────

class GateDb:
    def __init__(self, sub=None, business=None):
        self.sub, self.business = sub, business

    async def get(self, model, key, **_):
        return self.sub if model.__name__ == "Subscription" else self.business


def _state(sub, business=None):
    return asyncio.run(guards.plan_state(GateDb(sub, business), uuid.uuid4()))


def test_who_is_read_only_and_why(monkeypatch):
    monkeypatch.setattr(settings, "billing_enforced_from", "2026-10-13")
    new = SimpleNamespace(created_at=datetime(2026, 10, 14, tzinfo=UTC))
    old = SimpleNamespace(created_at=datetime(2026, 9, 1, tzinfo=UTC))
    assert _state(None, new) == "required"                 # signed up after billing began, never paid
    assert _state(None, old) is None                       # earlier customers are untouched
    assert _state(_sub(status="suspended")) == "suspended"
    assert _state(_sub(status="cancelled", current_period_end=datetime.now(UTC) - timedelta(days=1))) == "ended"
    assert _state(_sub(status="active")) is None
    assert _state(_sub(status="cancelled", current_period_end=datetime.now(UTC) + timedelta(days=5))) is None


def test_billing_is_off_by_default_until_a_start_date_is_set(monkeypatch):
    monkeypatch.setattr(settings, "billing_enforced_from", "")
    assert _state(None, SimpleNamespace(created_at=datetime(2030, 1, 1, tzinfo=UTC))) is None


def test_ai_calls_stop_for_a_paused_plan_or_an_empty_wallet(monkeypatch):
    async def wallet_blocked(db, business_id):
        return wallet_state["blocked"]

    wallet_state = {"blocked": False}
    monkeypatch.setattr(guards.wallet, "voice_blocked", wallet_blocked)
    paused = GateDb(_sub(status="suspended"))
    fine = GateDb(_sub())
    assert asyncio.run(guards.voice_calls_blocked(paused, uuid.uuid4())) is True
    assert asyncio.run(guards.voice_calls_blocked(fine, uuid.uuid4())) is False
    wallet_state["blocked"] = True
    assert asyncio.run(guards.voice_calls_blocked(fine, uuid.uuid4())) is True


def test_every_member_can_ask_whether_the_plan_is_paused_but_only_the_owner_can_manage_it():
    paused = GateDb(_sub(status="suspended"))
    as_agent = SimpleNamespace(business=uuid.uuid4(), role="agent")
    as_owner = SimpleNamespace(business=uuid.uuid4(), role="owner")
    agent_view = asyncio.run(billing_router.plan_status(as_agent, paused))
    owner_view = asyncio.run(billing_router.plan_status(as_owner, paused))
    assert agent_view.blocked and agent_view.state == "suspended" and agent_view.can_manage is False
    assert owner_view.can_manage is True


# ── automations on a paused plan ─────────────────────────────────────────────

def _run_step(monkeypatch, action_type, paused):
    async def blocked(db, business_id):
        return paused

    monkeypatch.setattr(guards, "plan_blocked", blocked)

    async def tag(*a, **k):
        return True

    monkeypatch.setattr(actions, "_add_tag", tag)
    business = SimpleNamespace(id=uuid.uuid4())
    customer = SimpleNamespace(id=uuid.uuid4(), paid_by_customer_id=None)
    return asyncio.run(actions._run_step_action(
        None, business=business, customer=customer, business_id=business.id, customer_id=customer.id,
        call_id=None, channel="whatsapp", trigger_type="message.received", context={}, action_type=action_type,
        action_config={"message": "hi", "tag": "x"}, log_ref="t",
    ))


def test_a_paused_plan_switches_off_steps_that_reach_customers(monkeypatch):
    with pytest.raises(actions.NoRecipient):
        _run_step(monkeypatch, "whatsapp_followup", paused=True)
    with pytest.raises(actions.NoRecipient):
        _run_step(monkeypatch, "place_call", paused=True)


def test_a_paused_plan_leaves_internal_steps_working(monkeypatch):
    assert _run_step(monkeypatch, "add_tag", paused=True) is True


def test_internal_steps_are_exactly_the_ones_that_cost_nothing():
    assert actions.INTERNAL_ACTIONS == {"create_escalation_task", "add_tag", "set_stage", "notify_team", "assign_to_agent"}


# ── the two doors PayU knocks on: no login, only a transaction id ────────────

def _client(monkeypatch, outcome="success"):
    from fastapi.testclient import TestClient

    from services.api.main import app
    from shared.db.session import get_db

    settled = []

    async def settle(db, txnid):
        settled.append(txnid)
        return SimpleNamespace(status=outcome) if txnid != "UNKNOWN" else None

    async def fake_db():
        yield SimpleNamespace()

    monkeypatch.setattr(service, "settle_payment", settle)
    monkeypatch.setattr(settings, "frontend_base_url", "https://krova.space")
    app.dependency_overrides[get_db] = fake_db
    return TestClient(app, follow_redirects=False), settled, app


def test_the_browser_coming_back_from_payu_needs_no_login_and_lands_on_billing(monkeypatch):
    client, settled, app = _client(monkeypatch, "success")
    try:
        # PayU posts the browser back as a form; nothing in it is believed, only the transaction id is read
        res = client.post("/api/v1/billing/payu/return", data={"txnid": "KR123", "status": "success", "amount": "1.00"})
    finally:
        app.dependency_overrides.clear()
    assert res.status_code == 303 and res.headers["location"] == "https://krova.space/billing?payment=success"
    assert settled == ["KR123"]


def test_a_return_with_nothing_we_know_is_not_a_success(monkeypatch):
    client, settled, app = _client(monkeypatch)
    try:
        res = client.post("/api/v1/billing/payu/return", data={"txnid": "UNKNOWN"})
        empty = client.post("/api/v1/billing/payu/return", data={})
    finally:
        app.dependency_overrides.clear()
    assert res.headers["location"].endswith("payment=unknown") and empty.headers["location"].endswith("payment=unknown")


def test_the_webhook_takes_a_form_or_json_and_only_asks_payu(monkeypatch):
    client, settled, app = _client(monkeypatch)
    try:
        as_form = client.post("/api/v1/billing/payu/webhook", data={"txnid": "KR1", "status": "success"})
        as_json = client.post("/api/v1/billing/payu/webhook", json={"txnid": "KR2", "status": "success"})
        garbage = client.post("/api/v1/billing/payu/webhook", content=b"not json", headers={"content-type": "application/json"})
    finally:
        app.dependency_overrides.clear()
    assert as_form.json() == {"ok": True} and as_json.json() == {"ok": True}
    assert settled == ["KR1", "KR2"]
    assert garbage.json() == {"ok": False}


def test_a_forged_success_cannot_credit_anything(monkeypatch):
    """The webhook's body says 'success' and an amount; none of it reaches the payment - PayU is asked."""
    from fastapi.testclient import TestClient

    from services.api.main import app
    from shared.db.session import get_db

    asked = []

    async def verify(txnid):
        asked.append(txnid)
        return None          # PayU has never heard of it

    payment = SimpleNamespace(
        business_id=uuid.uuid4(), purpose="topup", total_paise=118_000, base_paise=100_000, gst_paise=18_000,
        txnid="KRFAKE", status="pending", payu_id=None, mode=None, failure=None, raw={}, paid_at=None,
        created_at=datetime.now(UTC),
    )

    async def fake_db():
        yield OnePayment(payment)

    monkeypatch.setattr(service.payu, "verify_payment", verify)
    app.dependency_overrides[get_db] = fake_db
    try:
        TestClient(app).post("/api/v1/billing/payu/webhook", data={"txnid": "KRFAKE", "status": "success", "amt": "1180.00"})
    finally:
        app.dependency_overrides.clear()
    assert asked == ["KRFAKE"] and payment.status == "pending" and payment.paid_at is None
