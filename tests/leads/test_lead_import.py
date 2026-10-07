import io
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

import openpyxl  # noqa: E402

from shared.leads.justdial_parse import parse_lead  # noqa: E402
from shared.leads.lead_file import parse_lead_file  # noqa: E402
from shared.utils.log_redaction import mask_path  # noqa: E402

TOKEN = "AbCdEf0123456789XyZ_-AbCdEf0123456789"


def test_token_is_masked_in_webhook_paths():
    assert mask_path(f"/webhooks/justdial/{TOKEN}") == "/webhooks/justdial/***"
    assert mask_path(f"/webhooks/leads/magicbricks/{TOKEN}") == "/webhooks/leads/magicbricks/***"
    assert mask_path(f"/webhooks/indiamart/{TOKEN}") == "/webhooks/indiamart/***"


def test_normal_paths_are_untouched():
    assert mask_path("/api/v1/receivables/history") == "/api/v1/receivables/history"
    assert mask_path("/webhooks/justdial/short") == "/webhooks/justdial/short"


def test_csv_lead_export_maps_to_lead_fields():
    csv_text = "Name,Mobile,Email,Query\nRavi,9876543210,ravi@example.com,2BHK in Noida\n,,,\n"
    rows = parse_lead_file("export.csv", csv_text.encode("utf-8"))
    assert len(rows) == 1
    lead = parse_lead(rows[0])
    assert lead.name == "Ravi"
    assert lead.phone == "9876543210"
    assert lead.email == "ravi@example.com"
    assert lead.query == "2BHK in Noida"


def test_more_header_aliases_are_recognised():
    csv_text = "Contact Person,Contact Number,Requirement Details\nPriya,9988776655,Office space\n"
    rows = parse_lead_file("export.csv", csv_text.encode("utf-8"))
    lead = parse_lead(rows[0])
    assert lead.name == "Priya"
    assert lead.phone == "9988776655"
    assert lead.query == "Office space"


def test_xlsx_lead_export_maps_to_lead_fields():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Customer Name", "Phone", "Requirement"])
    ws.append(["Meena", "9123456780", "Plot in Greater Noida"])
    buf = io.BytesIO()
    wb.save(buf)
    rows = parse_lead_file("export.xlsx", buf.getvalue())
    lead = parse_lead(rows[0])
    assert lead.name == "Meena"
    assert lead.phone == "9123456780"
