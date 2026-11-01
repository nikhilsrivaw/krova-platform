"""
The "always a person" floor no longer stops what the agent can answer: on a business with Bookings, "kal ka slot hai
kya" and "booking cancel kar do" reach the AI (and its confirm-then-do change); without Bookings they still go to a
person. Money back, police, a payment the customer says they made, a reaction - always a person.
"""

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.ai import agent  # noqa: E402
from shared.ai import context as ctx  # noqa: E402
from shared.ai.auto_send_gate import floor_keywords, must_escalate  # noqa: E402

SALON = ["conversation_intelligence", "scheduling", "opd_queue", "lead_capture"]
SHOP = ["conversation_intelligence", "order_sync"]
PLAIN = ["conversation_intelligence", "lead_capture"]


@pytest.mark.parametrize("said", ["kal 4 baje ka slot hai kya", "Sunday ko slots available hain?",
                                  "meri booking cancel kar do", "appointment reschedule karna hai", "बुकिंग है क्या"])
def test_booking_words_reach_the_ai_when_it_can_see_bookings(said):
    assert must_escalate(said, SALON) is None
    assert must_escalate(said, PLAIN) and must_escalate(said) is not None


@pytest.mark.parametrize("said", ["mera order kab aayega", "order status batao", "order cancel karna hai"])
def test_order_words_reach_the_ai_when_it_can_see_orders(said):
    assert must_escalate(said, SHOP) is None and must_escalate(said, PLAIN)


@pytest.mark.parametrize("said", ["refund chahiye", "police complaint karunga", "maine payment kiya tha",
                                  "rash ho gaya", "fraud hai ye", "staff bahut rude tha"])
def test_some_things_always_need_a_person(said):
    for caps in (SALON, SHOP, PLAIN, None):
        assert must_escalate(said, caps)


def test_unknown_features_keep_every_group():
    assert len(floor_keywords(None)) > len(floor_keywords(SALON)) > 0


def _context(said, capabilities):
    return ctx.AgentContext(
        business_name="Shop", vertical="local_service", dna_summary=None, tone=None, policies=None, known_gaps=[],
        escalate_immediately=[], offerings={}, pricing_notes=None, opening_hours={}, availability=None,
        upcoming_bookings=None, cases=None, orders=None, properties=None, claims=None, open_shifts=None,
        customer_name="Ravi", customer_summary=None, customer_since=None, identities=[],
        recent=[{"channel": "voice", "direction": "inbound", "text": said}], channel="voice", capabilities=capabilities,
    )


def test_a_call_on_a_booking_business_keeps_the_ais_reply():
    reply = "REPLY\nCANCEL_BOOKING=2026-11-03T16:00:00+05:30\n\nKal 4 baje wali booking cancel kar doon?"
    assert agent._escalate_floor(reply, _context("meri booking cancel kar do", SALON)) == reply
    assert agent._escalate_floor(reply, _context("meri booking cancel kar do", PLAIN)).startswith("ESCALATE")
    assert agent._escalate_floor("REPLY\n\nOk", _context("refund chahiye", SALON)).startswith("ESCALATE")


def test_switched_on_changes_win_over_an_older_never_cancel_rule():
    c = _context("order cancel kar do", SHOP)
    c.policies = "Never confirm or cancel a Cash-on-Delivery order yourself."
    c.actions = ["cancel_order"]
    text = "\n".join(c._business_lines() + c._live_lines())
    assert "These are allowed even where a rule above says never" in text
    c.actions = []
    assert "These are allowed" not in "\n".join(c._business_lines() + c._live_lines())
