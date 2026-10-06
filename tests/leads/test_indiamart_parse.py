import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.leads.indiamart_parse import parse_indiamart  # noqa: E402


def test_documented_fields_map_to_the_shared_lead():
    lead = parse_indiamart({
        "UNIQUE_QUERY_ID": "IM-5521",
        "QUERY_TYPE": "W",
        "QUERY_TIME": "2026-10-06 10:15:00",
        "SENDER_NAME": "Anil Traders",
        "SENDER_MOBILE": "9876543210",
        "SENDER_EMAIL": "anil@example.com",
        "SENDER_CITY": "Kanpur",
        "QUERY_PRODUCT_NAME": "Industrial pumps",
        "QUERY_MESSAGE": "Need 5 pumps for a plant",
    })
    assert lead.external_id == "IM-5521"
    assert lead.name == "Anil Traders"
    assert lead.phone == "9876543210"
    assert lead.email == "anil@example.com"
    assert lead.query == "Industrial pumps | Need 5 pumps for a plant | city: Kanpur"


def test_phone_falls_back_to_alternate_fields():
    lead = parse_indiamart({"UNIQUE_QUERY_ID": "1", "SENDER_PHONE_ALT": "9123456789"})
    assert lead.phone == "9123456789"


def test_subject_is_used_when_message_is_missing():
    lead = parse_indiamart({"UNIQUE_QUERY_ID": "2", "SUBJECT": "Price enquiry"})
    assert lead.query == "Price enquiry"


def test_lead_with_no_contact_still_parses():
    lead = parse_indiamart({"UNIQUE_QUERY_ID": "3"})
    assert lead.phone is None
    assert lead.query is None
