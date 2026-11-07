"""The reply tool carries only the fields a business can use - the rest were paid for on every reply and never filled."""

import json
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.ai import agent  # noqa: E402

CORE = {"action", "message", "reasoning", "gap", "confidence"}


def _ctx(**kw):
    base = dict(availability=None, upcoming_bookings=None, properties=None, open_shifts=None, orders=None,
                capabilities=[], instagram_carousels=[], whatsapp_carousels=[], products=[])
    return SimpleNamespace(**{**base, **kw})


def _fields(ctx):
    return set(agent.reply_tool(ctx)["input_schema"]["properties"])


def test_a_business_with_nothing_extra_gets_only_the_core():
    assert _fields(_ctx()) == CORE


def test_a_college_with_counselling_keeps_booking_and_drops_the_rest():
    fields = _fields(_ctx(availability="Mon 10:00", upcoming_bookings=""))
    assert {"book_slot", "book_doctor", "reschedule_from", "reschedule_to", "cancel_appointment_at"} <= fields
    assert not fields & {"book_property", "book_token", "cancel_token", "share_carousel", "order_items", "cancel_order"}
    saved = len(json.dumps(agent.REPLY_TOOL)) - len(json.dumps(agent.reply_tool(_ctx(availability="x", upcoming_bookings=""))))
    assert saved > 3000                                   # characters, roughly 900 tokens per reply


def test_each_feature_brings_its_own_fields_back():
    assert {"book_token", "cancel_token"} <= _fields(_ctx(open_shifts=""))
    assert "book_property" in _fields(_ctx(properties=""))
    assert {"share_carousel", "share_carousel_values"} <= _fields(_ctx(whatsapp_carousels=[{"name": "x"}]))
    assert {"order_items", "order_payment"} <= _fields(_ctx(products=[{"name": "Tea", "price": "10"}]))
    assert {"cancel_order", "share_catalog"} <= _fields(_ctx(orders=""))


def test_a_context_it_cannot_read_keeps_every_field():
    assert _fields(SimpleNamespace()) == set(agent.REPLY_TOOL["input_schema"]["properties"])


def test_the_full_tool_is_unchanged_and_required_fields_stay():
    tool = agent.reply_tool(_ctx())
    assert tool["input_schema"]["required"] == agent.REPLY_TOOL["input_schema"]["required"]
    assert tool["name"] == agent.REPLY_TOOL["name"]
    assert len(agent.REPLY_TOOL["input_schema"]["properties"]) == 19
