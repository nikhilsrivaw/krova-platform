import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from datetime import date  # noqa: E402

from shared.integrations.receivables import parse_csv  # noqa: E402


def test_valid_rows_parse_to_paise_and_dates():
    csv_text = (
        "customer,invoice_number,due_date,amount\n"
        "Sharma Traders,INV-1,2026-10-20,1500.50\n"
        "Verma Stores,INV-2,20/10/2026,\"2,000\"\n"
    )
    rows, errors = parse_csv(csv_text.encode("utf-8"))
    assert errors == []
    assert rows[0].amount_paise == 150050
    assert rows[0].due_date == date(2026, 10, 20)
    assert rows[1].amount_paise == 200000
    assert rows[1].due_date == date(2026, 10, 20)


def test_headers_are_case_and_space_tolerant():
    rows, errors = parse_csv(b" Customer , Invoice_Number ,Due_Date, Amount \nA,1,,5\n")
    assert errors == []
    assert rows[0].due_date is None
    assert rows[0].amount_paise == 500


def test_bad_rows_are_reported_and_good_rows_still_import():
    csv_text = (
        "customer,invoice_number,due_date,amount\n"
        ",INV-1,2026-10-20,100\n"
        "B,,2026-10-20,100\n"
        "C,INV-3,2026-10-20,abc\n"
        "D,INV-4,2026-10-20,0\n"
        "E,INV-5,31-31-2026,100\n"
        "F,INV-6,2026-10-20,250\n"
    )
    rows, errors = parse_csv(csv_text.encode("utf-8"))
    assert [r.invoice_number for r in rows] == ["INV-6"]
    assert [e.line for e in errors] == [2, 3, 4, 5, 6]


def test_missing_column_is_refused_with_its_name():
    rows, errors = parse_csv(b"customer,amount\nA,100\n")
    assert rows == []
    assert "invoice_number" in errors[0].reason
    assert "due_date" in errors[0].reason


def test_non_utf8_file_is_refused():
    rows, errors = parse_csv("customer\n\xff\xfe".encode("latin-1"))
    assert rows == [] and errors
