"""
Payment links from the business's own Razorpay: the adapter's requests, how gateway answers become ledger payments
(never twice), and the sending rules (24-hour window, template, nothing sent without a working key).
No network: Razorpay's HTTP and the WhatsApp client are replaced.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from shared.db.models import CommitmentDirection, CommitmentStatus, ConnectionStatus
from shared.payments import gateways, links

NOW = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)


# ── the Razorpay adapter ─────────────────────────────────────────────────────

class _Http:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        code, body = self.responses.pop(0)
        return httpx.Response(code, json=body, request=httpx.Request(method, url))


def _patch_http(monkeypatch, *responses):
    fake = _Http(responses)
    monkeypatch.setattr(gateways.httpx, "AsyncClient", fake)
    return fake


def test_a_link_is_created_with_the_amount_in_paise_and_no_razorpay_sms(monkeypatch):
    http = _patch_http(monkeypatch, (200, {"id": "plink_1", "short_url": "https://rzp.io/i/abc", "status": "created",
                                           "amount": 50000, "amount_paid": 0, "expire_by": 1761400000}))
    info = asyncio.run(gateways.Razorpay("rzp_live_x", "s").create_link(
        amount_paise=50000, description="Fee for token 14", customer_name="Ramesh", customer_phone="+91 98765 43210", now=NOW,
    ))
    method, url, kwargs = http.calls[0]
    body = kwargs["json"]
    assert (method, url) == ("POST", "https://api.razorpay.com/v1/payment_links")
    assert kwargs["auth"] == ("rzp_live_x", "s")
    assert body["amount"] == 50000 and body["currency"] == "INR"
    assert body["notify"] == {"sms": False, "email": False}
    assert len(body["reference_id"]) <= 40
    assert body["customer"] == {"name": "Ramesh", "contact": "+919876543210"}
    assert body["expire_by"] == int((NOW + timedelta(days=7)).timestamp())
    assert info.external_id == "plink_1" and info.short_url == "https://rzp.io/i/abc"


def test_every_link_gets_its_own_reference(monkeypatch):
    ok = (200, {"id": "p", "short_url": "u", "status": "created", "amount": 100, "amount_paid": 0})
    http = _patch_http(monkeypatch, ok, ok)
    rp = gateways.Razorpay("k", "s")
    for _ in range(2):
        asyncio.run(rp.create_link(amount_paise=100, description="x", customer_name=None, customer_phone=None))
    refs = [c[2]["json"]["reference_id"] for c in http.calls]
    assert refs[0] != refs[1]


def test_less_than_one_rupee_is_refused_before_calling_razorpay(monkeypatch):
    http = _patch_http(monkeypatch)
    with pytest.raises(gateways.GatewayError):
        asyncio.run(gateways.Razorpay("k", "s").create_link(amount_paise=50, description="x", customer_name=None, customer_phone=None))
    assert http.calls == []


def test_a_wrong_key_says_so_plainly(monkeypatch):
    _patch_http(monkeypatch, (401, {"error": {"description": "Authentication failed"}}))
    with pytest.raises(gateways.GatewayError, match="Key ID and Secret"):
        asyncio.run(gateways.Razorpay("k", "bad").check())


def test_fetch_reads_status_and_amount_paid(monkeypatch):
    http = _patch_http(monkeypatch, (200, {"id": "plink_1", "short_url": "u", "status": "paid", "amount": 50000, "amount_paid": 50000}))
    info = asyncio.run(gateways.Razorpay("k", "s").fetch_link("plink_1"))
    assert http.calls[0][1] == "https://api.razorpay.com/v1/payment_links/plink_1"
    assert (info.status, info.amount_paid_paise) == ("paid", 50000)


def test_an_unknown_gateway_is_refused():
    with pytest.raises(gateways.GatewayError):
        gateways.gateway_for("cashfree", "k", "s")


# ── gateway answers become ledger payments, once ─────────────────────────────

def _commitment(amount=50000, received=0, status=CommitmentStatus.open):
    return SimpleNamespace(
        id=uuid.uuid4(), amount_paise=amount, amount_received_paise=received, payment_log=[], status=status,
        resolved_at=None, direction=CommitmentDirection.they_owe,
    )


def _link(recorded=0):
    return SimpleNamespace(id=uuid.uuid4(), gateway="razorpay", status="created", amount_paid_paise=0,
                           recorded_paise=recorded, paid_at=None, last_checked_at=None)


def test_a_paid_link_records_the_money_and_closes_the_fee():
    link, c = _link(), _commitment()
    assert links.apply_status(link, c, status="paid", amount_paid_paise=50000, now=NOW) == 50000
    assert c.amount_received_paise == 50000 and c.status == CommitmentStatus.met
    assert c.payment_log[-1]["source"] == "razorpay_link"
    assert link.paid_at == NOW and link.recorded_paise == 50000


def test_checking_again_never_records_twice():
    link, c = _link(), _commitment()
    links.apply_status(link, c, status="paid", amount_paid_paise=50000, now=NOW)
    assert links.apply_status(link, c, status="paid", amount_paid_paise=50000, now=NOW) == 0
    assert c.amount_received_paise == 50000 and len(c.payment_log) == 1


def test_a_part_payment_records_only_the_new_part():
    link, c = _link(), _commitment()
    links.apply_status(link, c, status="partially_paid", amount_paid_paise=20000, now=NOW)
    assert c.status == CommitmentStatus.open and c.amount_received_paise == 20000
    links.apply_status(link, c, status="paid", amount_paid_paise=50000, now=NOW)
    assert c.amount_received_paise == 50000 and c.status == CommitmentStatus.met


def test_nothing_paid_changes_nothing():
    link, c = _link(), _commitment()
    assert links.apply_status(link, c, status="created", amount_paid_paise=0, now=NOW) == 0
    assert c.amount_received_paise == 0 and link.last_checked_at == NOW


def test_a_fee_already_marked_paid_by_hand_is_not_paid_again():
    link, c = _link(), _commitment(status=CommitmentStatus.met)
    assert links.apply_status(link, c, status="paid", amount_paid_paise=50000, now=NOW) == 0
    assert c.amount_received_paise == 0 and link.recorded_paise == 50000


# ── sending rules ────────────────────────────────────────────────────────────

BIZ = uuid.uuid4()


class _Db:
    """Answers the few lookups send_link makes, in order."""

    def __init__(self, *, gateway=True, phone="+919876543210", last_inbound=NOW, template=None):
        self.gateway = SimpleNamespace(business_id=BIZ, gateway="razorpay", key_id="k", key_secret="enc",
                                       status=ConnectionStatus.active) if gateway else None
        self.phone, self.last_inbound, self.template = phone, last_inbound, template
        self.wa = SimpleNamespace(id=uuid.uuid4(), access_token="enc", external_account_id="pn")
        self.customer = SimpleNamespace(id=uuid.uuid4(), display_name="Ramesh", is_private=False)
        self.added = []

    async def get(self, model, key):
        return self.customer

    async def execute(self, statement):
        sql = str(statement)
        if "payment_gateway_connections" in sql:
            value = self.gateway
        elif "customer_identities" in sql:
            value = self.phone
        elif "channel_connections" in sql:
            value = self.wa
        elif "messages.occurred_at" in sql:
            value = self.last_inbound
        elif "message_templates" in sql:
            value = self.template
        else:
            raise AssertionError(sql)
        return SimpleNamespace(scalar_one_or_none=lambda: value,
                               scalars=lambda: SimpleNamespace(first=lambda: value))

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        pass


def _owed(amount=50000):
    return SimpleNamespace(id=uuid.uuid4(), business_id=BIZ, customer_id=uuid.uuid4(), description="Fee for token 14",
                           direction=CommitmentDirection.they_owe, status=CommitmentStatus.open, outstanding_paise=amount)


@pytest.fixture
def wiring(monkeypatch):
    sent = {}

    class Gateway:
        async def create_link(self, **kwargs):
            sent["create"] = kwargs
            return gateways.LinkInfo("plink_1", "https://rzp.io/i/abc", "created", kwargs["amount_paise"], 0, NOW)

    class Wa:
        def __init__(self, *a):
            pass

        async def send_text(self, to, text):
            sent["text"] = (to, text)
            return SimpleNamespace(external_id="wamid.1")

        async def send_template(self, to, name, language, body_params=None):
            sent["template"] = (to, name, body_params)
            return SimpleNamespace(external_id="wamid.2")

    async def ingest(**kwargs):
        sent["ingest"] = kwargs

    monkeypatch.setattr(links, "_client", lambda row: Gateway())
    monkeypatch.setattr(links, "WhatsAppClient", Wa)
    monkeypatch.setattr(links, "decrypt", lambda v: v)
    monkeypatch.setattr(links.ingest, "ingest", ingest)
    return sent


def test_inside_24_hours_the_link_goes_as_a_plain_message(wiring):
    db = _Db(last_inbound=datetime.now(timezone.utc) - timedelta(hours=2))
    link, ok, reason = asyncio.run(links.send_link(db, business=SimpleNamespace(id=BIZ), commitment=_owed(), user_id=None))
    assert ok and reason is None
    to, text = wiring["text"]
    assert to == "+919876543210" and "Rs 500" in text and "https://rzp.io/i/abc" in text
    assert wiring["ingest"]["text"] == text and link.external_id == "plink_1"


def test_after_24_hours_a_template_is_required(wiring):
    db = _Db(last_inbound=datetime.now(timezone.utc) - timedelta(days=3))
    with pytest.raises(links.LinkError, match="24 hours"):
        asyncio.run(links.send_link(db, business=SimpleNamespace(id=BIZ), commitment=_owed(), user_id=None))
    assert "create" not in wiring  # nothing created in Razorpay for a message that cannot go


def test_after_24_hours_an_approved_template_carries_amount_and_link(wiring):
    from shared.db.models import TemplateStatus
    template = SimpleNamespace(name="payment_link", language="en", status=TemplateStatus.approved,
                               body_text="Your payment of {{1}} is due. Pay here: {{2}}")
    db = _Db(last_inbound=None, template=template)
    _, ok, _ = asyncio.run(links.send_link(db, business=SimpleNamespace(id=BIZ), commitment=_owed(),
                                           user_id=None, template_name="payment_link"))
    assert ok and wiring["template"][2] == ["Rs 500", "https://rzp.io/i/abc"]
    assert wiring["ingest"]["text"] == "Your payment of Rs 500 is due. Pay here: https://rzp.io/i/abc"


def test_no_gateway_no_link(wiring):
    with pytest.raises(links.LinkError, match="payment gateway"):
        asyncio.run(links.send_link(_Db(gateway=False), business=SimpleNamespace(id=BIZ), commitment=_owed(), user_id=None))


def test_nothing_owed_no_link(wiring):
    paid = _owed(amount=0)
    with pytest.raises(links.LinkError):
        asyncio.run(links.send_link(_Db(), business=SimpleNamespace(id=BIZ), commitment=paid, user_id=None))


def test_no_phone_still_makes_the_link_to_share_another_way(wiring):
    db = _Db(phone=None)
    link, ok, reason = asyncio.run(links.send_link(db, business=SimpleNamespace(id=BIZ), commitment=_owed(), user_id=None))
    assert not ok and "phone" in reason and link.short_url == "https://rzp.io/i/abc"


def test_rupees_reads_naturally():
    assert links.rupees(50000) == "Rs 500" and links.rupees(150050) == "Rs 1,500.50"
