"""A quiet gap ends a conversation: only the new one is "the conversation so far", the one before it is shown dated
as background, and what the customer is asking about is read from the new one alone."""

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.ai import context as ctx  # noqa: E402

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _msgs(*ages_hours):
    """Messages newest first, as build() reads them, each `age` hours before NOW."""
    return [SimpleNamespace(id=i, occurred_at=NOW - timedelta(hours=h)) for i, h in enumerate(ages_hours)]


def test_a_long_gap_starts_a_new_conversation():
    current, earlier = ctx.split_conversation(_msgs(0, 0.1, 0.2, 500, 500.1, 501))
    assert [m.id for m in current] == [2, 1, 0]
    assert [m.id for m in earlier] == [5, 4, 3]


def test_no_gap_keeps_one_conversation_up_to_the_cap():
    current, earlier = ctx.split_conversation(_msgs(*[i * 0.5 for i in range(26)]))
    assert len(current) == ctx.RECENT_TURNS and earlier == []
    assert current[-1].id == 0


def test_a_reply_the_next_morning_is_the_same_conversation():
    current, earlier = ctx.split_conversation(_msgs(0, 14, 15))
    assert len(current) == 3 and earlier == []


def test_earlier_is_capped():
    current, earlier = ctx.split_conversation(_msgs(0, *[100 + i for i in range(20)]))
    assert len(current) == 1 and len(earlier) == ctx.EARLIER_TURNS
    assert earlier[-1].id == 1


def _context(recent, earlier):
    return ctx.AgentContext(
        business_name="Aqirox", vertical="general", dna_summary=None, tone=None, policies=None, known_gaps=[],
        escalate_immediately=[], offerings={}, pricing_notes=None, opening_hours={}, availability=None,
        upcoming_bookings=None, cases=None, orders=None, properties=None, claims=None, open_shifts=None,
        customer_name="Ravi", customer_summary=None, customer_since=None, identities=[], recent=recent,
        earlier=earlier, channel="whatsapp",
    )


OLD = [
    {"channel": "whatsapp", "direction": "inbound", "text": "order kharab aaya, refund chahiye", "date": "30 Oct 2026"},
    {"channel": "whatsapp", "direction": "outbound", "text": "Sorry, team ko bata diya", "date": "30 Oct 2026"},
]
NEW = [{"channel": "whatsapp", "direction": "inbound", "text": "hi"}]


def test_the_earlier_conversation_is_shown_dated_and_apart():
    out = _context(NEW, OLD).render_conversation()
    assert out.index("An earlier conversation") < out.index("[30 Oct 2026] [whatsapp] Customer: order kharab aaya")
    assert out.index("order kharab aaya") < out.index("The conversation so far:")
    after = out.split("The conversation so far:")[1]
    assert "refund" not in after and "Customer: hi" in after


def test_the_topic_comes_from_this_conversation_only():
    assert _context(NEW, OLD).asked_topics() == set()
    assert "order" in _context([{"channel": "whatsapp", "direction": "inbound", "text": "mera order kab aayega"}], OLD).asked_topics()


def test_no_earlier_block_without_a_gap():
    out = _context(NEW, []).render_conversation()
    assert "An earlier conversation" not in out
    assert out.startswith("\n---\n\nThe conversation so far:")
