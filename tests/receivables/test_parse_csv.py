import io
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from datetime import date, datetime  # noqa: E402

import openpyxl  # noqa: E402

from shared.integrations.receivables import parse_file  # noqa: E402


def csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def test_template_columns_parse_to_paise_and_dates():
    text = (
        "customer,invoice_number,due_date,amount\n"
        "Sharma Traders,INV-1,2026-10-20,1500.50\n"
        "Verma Stores,INV-2,20/10/2026,\"2,000\"\n"
    )
    rows, errors = parse_file("a.csv", csv_bytes(text))
    assert errors == []
    assert rows[0].amount_paise == 150050
    assert rows[0].due_date == date(2026, 10, 20)
    assert rows[1].amount_paise == 200000


def test_tally_style_and_zoho_style_labels_are_recognised():
    text = (
        "Party Name,Bill No,Due On,Balance Due\n"
        "Sharma Traders,B-7,2026-10-20,900\n"
    )
    rows, errors = parse_file("a.csv", csv_bytes(text))
    assert errors == []
    assert rows[0].customer == "Sharma Traders"
    assert rows[0].invoice_number == "B-7"
    assert rows[0].amount_paise == 90000


def test_due_date_is_optional_and_headers_are_case_tolerant():
    rows, errors = parse_file("a.csv", csv_bytes(" Customer , Invoice Number , Amount \nA,1,5\n"))
    assert errors == []
    assert rows[0].due_date is None
    assert rows[0].amount_paise == 500


def test_bad_rows_are_reported_and_good_rows_still_import():
    text = (
        "customer,invoice_number,due_date,amount\n"
        ",INV-1,2026-10-20,100\n"
        "B,,2026-10-20,100\n"
        "C,INV-3,2026-10-20,abc\n"
        "D,INV-4,2026-10-20,0\n"
        "E,INV-5,31-31-2026,100\n"
        "F,INV-6,2026-10-20,250\n"
    )
    rows, errors = parse_file("a.csv", csv_bytes(text))
    assert [r.invoice_number for r in rows] == ["INV-6"]
    assert [e.line for e in errors] == [2, 3, 4, 5, 6]


def test_missing_required_column_is_named():
    rows, errors = parse_file("a.csv", csv_bytes("customer,amount\nA,100\n"))
    assert rows == []
    assert "invoice_number" in errors[0].reason


def test_non_utf8_csv_is_refused():
    try:
        parse_file("a.csv", "customer\n\xff\xfe".encode("latin-1"))
    except ValueError as exc:
        assert "UTF-8" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_xlsx_with_real_dates_and_numbers():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Customer Name", "Invoice No", "Due Date", "Balance"])
    ws.append(["Gupta Foods", "X-9", datetime(2026, 11, 1), 1234.5])
    buf = io.BytesIO()
    wb.save(buf)
    rows, errors = parse_file("report.xlsx", buf.getvalue())
    assert errors == []
    assert rows[0].customer == "Gupta Foods"
    assert rows[0].due_date == date(2026, 11, 1)
    assert rows[0].amount_paise == 123450
