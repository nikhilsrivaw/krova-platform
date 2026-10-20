"""The fee list on the Knowledge page - shared/payments/fee_list.py."""

import pytest

from shared.payments import fee_list
from shared.verticals import knowledge_lookup

ROWS = [
    {"id": "ct", "name": "CT scan", "amount_paise": 250000, "details": "head"},
    {"id": "xr", "name": "X-ray", "amount_paise": 40000},
]


def test_rows_are_cleaned_and_each_gets_an_id():
    rows = fee_list.clean_rows([
        {"name": "  CBC ", "amount_paise": 30000},
        {"name": "", "amount_paise": 100},          # no name
        {"name": "Free", "amount_paise": 0},        # below ₹1
        {"name": "Odd", "amount_paise": "abc"},     # not a number
        {"id": "x", "name": "A", "amount_paise": 100}, {"id": "x", "name": "B", "amount_paise": 100},  # same id twice
    ])
    assert [r["name"] for r in rows] == ["CBC", "A", "B"]
    assert all(r["id"] for r in rows) and len({r["id"] for r in rows}) == 3
    assert rows[0]["amount_paise"] == 30000 and rows[0]["details"] == ""


def test_pick_keeps_a_copy_in_the_order_ticked():
    items = fee_list.pick(fee_list.clean_rows(ROWS), ["xr", "ct"])
    assert items == [{"id": "xr", "name": "X-ray", "amount_paise": 40000},
                     {"id": "ct", "name": "CT scan", "amount_paise": 250000}]
    assert fee_list.total(items) == 290000 and fee_list.label(items) == "X-ray + CT scan"


def test_a_removed_fee_cannot_be_picked():
    with pytest.raises(fee_list.FeeError, match="no longer"):
        fee_list.pick(fee_list.clean_rows(ROWS), ["gone"])


def test_too_many_at_once_is_refused():
    with pytest.raises(fee_list.FeeError):
        fee_list.pick(fee_list.clean_rows(ROWS), ["ct"] * (fee_list.ITEMS_LIMIT + 1))


def test_nothing_ticked_is_none():
    import asyncio
    assert asyncio.run(fee_list.pick_for(None, None, [])) is None
    assert fee_list.total(None) == 0 and fee_list.label(None) == ""


def test_the_ai_finds_a_fee_by_the_customers_words():
    rows = fee_list.lookup_rows(fee_list.clean_rows(ROWS))
    found = knowledge_lookup.match_catalogue(rows, "CT scan kitne ka hai?")
    assert [r["name"] for r in found] == ["CT scan"] and found[0]["price"] == "₹2,500"
    assert knowledge_lookup.row_line(found[0]) == "CT scan - ₹2,500 (head)"


def test_rupees_shows_paise_only_when_there_are_some():
    assert fee_list.rupees(250000) == "₹2,500" and fee_list.rupees(12345) == "₹123.45"
