from shared.leads.justdial_parse import parse_lead


def test_flat_payload_with_common_keys():
    lead = parse_lead({
        "lead_id": "JD-1001",
        "name": "Ramesh Kumar",
        "mobile": "+91 98765 43210",
        "email": "ramesh@example.com",
        "query": "Banquet hall for 200 guests",
    })
    assert lead.external_id == "JD-1001"
    assert lead.name == "Ramesh Kumar"
    assert lead.phone == "+91 98765 43210"
    assert lead.email == "ramesh@example.com"
    assert lead.query == "Banquet hall for 200 guests"


def test_nested_data_object_is_read():
    lead = parse_lead({"data": {"Name": "Sunita", "Mobile": "9876543210", "Query": "Catering"}})
    assert lead.name == "Sunita"
    assert lead.phone == "9876543210"
    assert lead.query == "Catering"


def test_missing_fields_are_none_not_errors():
    lead = parse_lead({"name": "   "})
    assert lead.name is None
    assert lead.phone is None
    assert lead.external_id is None


def test_empty_payload_parses_to_all_none():
    lead = parse_lead({})
    assert (lead.name, lead.phone, lead.email, lead.query, lead.external_id) == (None,) * 5
