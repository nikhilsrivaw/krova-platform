import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from services.api.routers.lead_forms import _build_parsed_lead  # noqa: E402

FIELDS = [
    {"key": "full_name", "label": "Full name", "type": "name", "required": True},
    {"key": "mobile", "label": "Mobile number", "type": "phone", "required": True},
    {"key": "email_addr", "label": "Email", "type": "email", "required": False},
    {"key": "interest", "label": "What are you interested in?", "type": "select", "required": False,
     "options": ["Buying", "Renting"]},
    {"key": "notes", "label": "Anything else?", "type": "textarea", "required": False},
]


def test_typed_fields_map_onto_the_shared_lead_shape():
    parsed, stored = _build_parsed_lead(FIELDS, {
        "full_name": "Priya Shah", "mobile": "9876543210", "email_addr": "priya@example.com",
        "interest": "Renting", "notes": "Need a 2BHK",
    })
    assert parsed.name == "Priya Shah"
    assert parsed.phone == "9876543210"
    assert parsed.email == "priya@example.com"
    assert parsed.query == "What are you interested in?: Renting; Anything else?: Need a 2BHK"
    assert stored == {
        "full_name": "Priya Shah", "mobile": "9876543210", "email_addr": "priya@example.com",
        "interest": "Renting", "notes": "Need a 2BHK",
    }


def test_blank_values_are_skipped_not_stored_as_empty_query_lines():
    parsed, stored = _build_parsed_lead(FIELDS, {"full_name": "Raj", "mobile": "  "})
    assert parsed.name == "Raj"
    assert parsed.phone is None
    assert parsed.query is None
    assert stored["mobile"] == ""


def test_missing_values_dict_entries_do_not_crash():
    parsed, stored = _build_parsed_lead(FIELDS, {})
    assert parsed.name is None
    assert parsed.phone is None
    assert parsed.query is None
    assert all(v == "" for v in stored.values())


def test_only_the_first_field_of_a_reserved_type_is_used():
    # A business could define two "phone" fields by mistake - the first one
    # wins, the second becomes a query line instead of silently overwriting.
    fields = [
        {"key": "p1", "label": "Phone", "type": "phone", "required": True},
        {"key": "p2", "label": "Alternate phone", "type": "phone", "required": False},
    ]
    parsed, _ = _build_parsed_lead(fields, {"p1": "111", "p2": "222"})
    assert parsed.phone == "111"
    assert parsed.query == "Alternate phone: 222"


CONDITIONAL_FIELDS = [
    {"key": "interest", "label": "Interested in?", "type": "select", "required": True,
     "options": ["Buying", "Renting"]},
    {"key": "budget", "label": "Budget", "type": "text", "required": True,
     "show_if": {"field_key": "interest", "equals": "Buying"}},
]


def test_a_field_behind_show_if_is_skipped_when_the_condition_does_not_match():
    parsed, stored = _build_parsed_lead(CONDITIONAL_FIELDS, {"interest": "Renting", "budget": "50 lakh"})
    assert "Budget" not in (parsed.query or "")
    assert "budget" not in stored


def test_a_field_behind_show_if_is_included_when_the_condition_matches():
    parsed, stored = _build_parsed_lead(CONDITIONAL_FIELDS, {"interest": "Buying", "budget": "50 lakh"})
    assert parsed.query == "Interested in?: Buying; Budget: 50 lakh"
    assert stored["budget"] == "50 lakh"
