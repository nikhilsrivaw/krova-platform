import os

# Settings require these at import time; the values only need to exist.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ZOHO_CLIENT_ID", "test-client")

from datetime import date  # noqa: E402

from shared.integrations import zoho_books  # noqa: E402


def test_balance_becomes_paise():
    inv = zoho_books.parse_invoice({
        "invoice_id": "123", "invoice_number": "INV-1", "customer_name": "Sharma Traders",
        "due_date": "2026-10-20", "balance": "1500.50",
    })
    assert inv.balance_paise == 150050
    assert inv.due_date == date(2026, 10, 20)
    assert inv.customer_name == "Sharma Traders"


def test_fully_paid_invoice_is_dropped():
    assert zoho_books.parse_invoice({"invoice_id": "1", "balance": 0}) is None


def test_missing_id_or_balance_is_dropped():
    assert zoho_books.parse_invoice({"balance": 10}) is None
    assert zoho_books.parse_invoice({"invoice_id": "1"}) is None


def test_missing_customer_and_bad_date_still_parse():
    inv = zoho_books.parse_invoice({"invoice_id": "9", "balance": 5, "due_date": "not-a-date"})
    assert inv.customer_name == "Unknown customer"
    assert inv.due_date is None


def test_auth_url_carries_scope_state_and_redirect():
    url = zoho_books.build_auth_url("state-abc")
    assert url.startswith("https://accounts.zoho.in/oauth/v2/auth?")
    assert "state=state-abc" in url
    assert "access_type=offline" in url
    assert "ZohoBooks.invoices.READ" in url
    assert "api%2Fv1%2Fzoho%2Fcallback" in url or "api/v1/zoho/callback" in url
