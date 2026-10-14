"""
Invoices and receipts: the GST arithmetic, the numbering, who gets CGST+SGST or IGST, the PDF, and the
billing-details form. A scripted fake session stands in for the database (there is no local Postgres).
"""

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.api.routers import billing
from shared.billing import gst, invoice_pdf, invoices, supplier


def test_the_sellers_gstin_is_a_valid_one():
    assert gst.valid_gstin(supplier.GSTIN) and supplier.GSTIN.startswith(supplier.STATE_CODE)


@pytest.mark.parametrize("bad", ["", "09ABFCA3649E1ZA", "99ABFCA3649E1ZO", "09ABFCA3649E1YO", "short", "09ABFCA3649E1Z"])
def test_wrong_gstins_are_refused(bad):
    assert not gst.valid_gstin(bad)


def test_a_gstin_in_lower_case_is_accepted():
    assert gst.valid_gstin(supplier.GSTIN.lower())


@pytest.mark.parametrize("total,intra,expect", [
    (353882, True, (299900, 26991, 26991, 0)),
    (353882, False, (299900, 0, 0, 53982)),
    (707882, True, (599900, 53991, 53991, 0)),
    (1179882, False, (999900, 0, 0, 179982)),
])
def test_gst_is_taken_out_of_the_total_and_split_by_place(total, intra, expect):
    out = gst.split_gst(total, intra_state=intra)
    assert out == expect and sum(out) == total


def test_an_odd_amount_still_splits_cgst_and_sgst_equally():
    taxable, cgst, sgst, igst = gst.split_gst(100, intra_state=True)  # the Rs 1 test coupon
    assert cgst == sgst and taxable + cgst + sgst == 100 and igst == 0


@pytest.mark.parametrize("when,fy", [
    (datetime(2026, 3, 31, 18, 0, tzinfo=timezone.utc), "2526"),   # 31 Mar 23:30 India: still last year
    (datetime(2026, 3, 31, 19, 0, tzinfo=timezone.utc), "2627"),   # 1 Apr 00:30 India: new year
    (datetime(2026, 10, 14, tzinfo=timezone.utc), "2627"),
    (datetime(2027, 4, 1, tzinfo=timezone.utc), "2728"),
])
def test_the_financial_year_turns_over_on_1_april_india_time(when, fy):
    assert gst.financial_year(when) == fy


def test_numbers_are_short_enough_and_distinct_per_kind():
    a, b = invoices.number_for("invoice", "2627", 1), invoices.number_for("receipt", "2627", 1)
    assert a == "AQX/2627/000001" and b == "AQXR/2627/000001" and len(b) <= 16
    assert len(invoices.number_for("receipt", "2627", 999999)) <= 16


@pytest.mark.parametrize("paise,words", [
    (353882, "Rupees Three Thousand Five Hundred Thirty-Eight and Eighty-Two Paise only"),
    (102360, "Rupees One Thousand Twenty-Three and Sixty Paise only"),
    (10000000, "Rupees One Lakh only"),
    (100, "Rupees One only"),
])
def test_amounts_are_written_out_in_words(paise, words):
    assert gst.rupees_in_words(paise) == words


def test_money_uses_indian_digit_grouping():
    assert invoice_pdf.money(353882) == "₹3,538.82"
    assert invoice_pdf.money(12345678) == "₹1,23,456.78"


# ── creating the document for a payment ──────────────────────────────────────

class FakeDb:
    def __init__(self, existing=None, email="owner@shop.in", counter=1):
        self.existing, self.email, self.counter, self.added = existing, email, counter, []

    async def execute(self, stmt):
        sql = str(stmt.compile()).lower()
        if "invoice_counters" in sql:
            return SimpleNamespace(scalar_one=lambda: self.counter)
        if "from invoices" in sql:
            return SimpleNamespace(scalar_one_or_none=lambda: self.existing)
        return SimpleNamespace(scalar_one_or_none=lambda: self.email)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def business(**details):
    settings = {"billing_details": details} if details else {}
    return SimpleNamespace(id=uuid.uuid4(), name="Sharma Clinic", settings=settings)


def payment(purpose="plan", **kw):
    base = dict(
        id=uuid.uuid4(), purpose=purpose, plan="starter", base_paise=299900, gst_paise=53982, fee_paise=0,
        total_paise=353882, mode="UPI", txnid="KRVA1", paid_at=datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def make(db, pay, biz, **kw):
    return asyncio.run(invoices.create_for_payment(db, pay, business=biz, **kw))


def test_a_plan_payment_in_the_same_state_gets_cgst_and_sgst():
    db = FakeDb()
    inv = make(db, payment(), business(legal_name="Sharma Dental Clinic", address="Lucknow", state_code="09"))
    assert inv.kind == "invoice" and inv.number == "AQX/2627/000001" and db.added == [inv]
    assert (inv.taxable_paise, inv.cgst_paise, inv.sgst_paise, inv.igst_paise, inv.total_paise) == (299900, 26991, 26991, 0, 353882)
    assert inv.buyer_name == "Sharma Dental Clinic" and inv.buyer_email == "owner@shop.in"
    assert inv.lines[0]["description"] == "KROVA Starter plan - monthly subscription"


def test_a_buyer_in_another_state_gets_igst():
    inv = make(FakeDb(), payment(), business(legal_name="B", address="x", state_code="29"))
    assert (inv.cgst_paise, inv.sgst_paise, inv.igst_paise) == (0, 0, 53982) and inv.buyer_state_code == "29"


def test_a_gstin_on_file_is_printed_and_decides_the_state():
    inv = make(FakeDb(), payment(), business(legal_name="Karnataka Co", gstin="29AAACH7409R1ZX"))
    assert inv.buyer_gstin == "29AAACH7409R1ZX" and inv.buyer_state_code == "29" and inv.igst_paise > 0


def test_without_billing_details_it_falls_back_to_the_business_name_and_the_sellers_state():
    inv = make(FakeDb(), payment(), business())
    assert inv.buyer_name == "Sharma Clinic" and inv.buyer_state_code == "09" and inv.igst_paise == 0


def test_a_wallet_top_up_gets_a_receipt_with_no_gst():
    pay = payment("topup", plan=None, base_paise=100000, gst_paise=0, fee_paise=2360, total_paise=102360)
    inv = make(FakeDb(), pay, business())
    assert inv.kind == "receipt" and inv.number == "AQXR/2627/000001"
    assert (inv.taxable_paise, inv.cgst_paise, inv.sgst_paise, inv.igst_paise) == (0, 0, 0, 0)
    assert [row["amount_paise"] for row in inv.lines] == [100000, 2360] and inv.fee_paise == 2360 and inv.total_paise == 102360


def test_a_renewal_records_its_service_period():
    start, end = datetime(2026, 11, 14, tzinfo=timezone.utc), datetime(2026, 12, 14, tzinfo=timezone.utc)
    inv = make(FakeDb(), payment("renewal"), business(), period_start=start, period_end=end)
    assert inv.service_from == start and inv.service_to == end and inv.next_renewal == end


def test_asking_twice_for_the_same_payment_gives_the_same_document():
    first = make(FakeDb(), payment(), business())
    db = FakeDb(existing=first)
    assert make(db, payment(id=first.payment_id), business()) is first and db.added == []


# ── the PDF ──────────────────────────────────────────────────────────────────

def test_every_kind_of_document_renders_a_pdf():
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    plan = make(FakeDb(), payment(), business(legal_name="A", address="x", state_code="09"), period_start=now, period_end=now)
    inter = make(FakeDb(), payment(), business(legal_name="B", address="y", state_code="29"), period_start=now, period_end=now)
    receipt = make(FakeDb(), payment("topup", plan=None, base_paise=100000, gst_paise=0, fee_paise=2360, total_paise=102360), business())
    for doc in (plan, inter, receipt):
        pdf = invoice_pdf.render(doc)
        assert pdf.startswith(b"%PDF") and len(pdf) > 5000
    assert invoice_pdf.filename(plan) == "AQX-2627-000001.pdf"


# ── billing details and downloads (routes) ───────────────────────────────────

class RouteDb:
    def __init__(self, biz, invoice=None):
        self.biz, self.invoice = biz, invoice

    async def get(self, model, key):
        return self.biz if model.__name__ == "Business" else self.invoice

    async def flush(self):
        pass


def who(biz):
    return SimpleNamespace(business=biz.id, id=uuid.uuid4(), role="owner")


def save(biz, **body):
    return asyncio.run(billing.save_billing_details(billing.BillingDetailsIn(**body), who(biz), RouteDb(biz)))


def test_saving_billing_details_keeps_them_for_the_next_invoice():
    biz = business()
    out = save(biz, legal_name=" Sharma Dental ", address="Lucknow", state_code="09")
    assert out.complete and out.state_name == "Uttar Pradesh"
    assert biz.settings["billing_details"] == {"legal_name": "Sharma Dental", "address": "Lucknow", "state_code": "09", "gstin": ""}


def test_a_gstin_sets_the_state_by_itself():
    out = save(business(), legal_name="K", address="Bengaluru", gstin="29aaach7409r1zx")
    assert out.state_code == "29" and out.gstin == "29AAACH7409R1ZX"


@pytest.mark.parametrize("body", [
    dict(legal_name="K", address="x", state_code="09", gstin="29AAACH7409R1ZZ"),  # checksum wrong
    dict(legal_name="K", address="x", state_code=""),                              # no state, no gstin
    dict(legal_name="K", address="x", state_code="55"),                            # not a GST state
])
def test_bad_billing_details_are_refused(body):
    with pytest.raises(HTTPException) as e:
        save(business(), **body)
    assert e.value.status_code == 422


def test_an_invoice_can_only_be_downloaded_by_the_business_it_belongs_to():
    biz, other = business(), business()
    inv = make(FakeDb(), payment(), biz)
    inv.business_id = other.id
    with pytest.raises(HTTPException) as e:
        asyncio.run(billing.download_invoice(inv.id, who(biz), RouteDb(biz, inv)))
    assert e.value.status_code == 404
    inv.business_id = biz.id
    res = asyncio.run(billing.download_invoice(inv.id, who(biz), RouteDb(biz, inv)))
    assert res.media_type == "application/pdf" and "AQX-2627-000001.pdf" in res.headers["content-disposition"]
