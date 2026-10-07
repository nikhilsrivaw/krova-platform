import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from email.message import EmailMessage  # noqa: E402

from shared.leads.email_parse import (  # noqa: E402
    extract_lead_from_body,
    parse_raw_email,
    recipient_local_part,
)


def build_email(to: str, subject: str, body: str, message_id: str = "<abc@portal.example>") -> bytes:
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = "alerts@someportal.example"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg.set_content(body)
    return bytes(msg)


def test_labelled_fields_are_read_from_the_body():
    raw = build_email(
        "leads-abc123@leads.krova.space",
        "New enquiry",
        "Name: Suresh Kumar\nMobile: 9876500000\nEmail: suresh@example.com\nMessage: Looking for a 3BHK\n",
    )
    email = parse_raw_email(raw)
    assert email.message_id == "<abc@portal.example>"
    lead = extract_lead_from_body(email.body_text)
    assert lead.name == "Suresh Kumar"
    assert lead.phone == "9876500000"
    assert lead.email == "suresh@example.com"
    assert lead.query == "Looking for a 3BHK"


def test_unlabelled_phone_number_is_still_found():
    raw = build_email(
        "leads-abc123@leads.krova.space", "Lead alert",
        "A buyer is interested in your listing. Call them on 9123456789 to follow up.",
    )
    email = parse_raw_email(raw)
    lead = extract_lead_from_body(email.body_text)
    assert lead.phone == "9123456789"


def test_recipient_local_part_handles_display_name_format():
    assert recipient_local_part("Leads <leads-xyz789@leads.krova.space>") == "leads-xyz789"
    assert recipient_local_part("leads-xyz789@leads.krova.space") == "leads-xyz789"
    assert recipient_local_part(None) is None


def test_no_phone_anywhere_leaves_it_none():
    raw = build_email("leads-abc123@leads.krova.space", "Hi", "Just saying hello, no number here.")
    email = parse_raw_email(raw)
    lead = extract_lead_from_body(email.body_text)
    assert lead.phone is None
