"""The reply's language and script: decided in code from the customer's last message (or the site's "Replies in"),
and a reply that comes back in the wrong script is written once more."""

import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.ai import agent as agent_module  # noqa: E402
from shared.ai import context as ctx  # noqa: E402
from shared.ai import reply_language as rl  # noqa: E402
from shared.channels.web import reply as web_reply  # noqa: E402

DEVA = "लैपटॉप रीसेलिंग बिज़नेस के लिए WhatsApp ऑटोमेशन बना सकते हैं"


@pytest.mark.parametrize("said, choice, want", [
    ("what services your company offers ??", "auto", "latin"), ("hi", "auto", "latin"),
    ("kal 1 baje aa jaunga, theek hai?", "auto", "latin"), ("आपकी फीस कितनी है", "auto", "devanagari"),
    ("आपकी फीस कितनी है", "en", "latin"), ("what is the price", "hi", "devanagari"), ("👍", "auto", None),
])
def test_the_script_is_read_from_their_last_message_or_the_rule(said, choice, want):
    assert rl.expected(said, choice) == want


def test_the_hint_names_the_script():
    assert "English letters only" in rl.hint("what services do you offer?") and "even if earlier" in rl.hint("hello")
    assert "Hinglish" in rl.hint("aapka price kya hai bhai")
    assert "Devanagari Hindi" in rl.hint("आपकी फीस कितनी है")
    assert "set by the business" in rl.hint("आपकी फीस", "en") and rl.hint("👍") is None
    assert rl.hint("hi", "junk") == rl.hint("hi", "auto")


@pytest.mark.parametrize("reply, want, wrong", [
    (DEVA, "latin", True), ("We build WhatsApp automation for ₹4,999/month.", "latin", False),
    ("Hum WhatsApp automation banate hain, kal baat karte hain?", "latin", False),
    ("We build WhatsApp automation for your laptop business.", "devanagari", True),
    (DEVA, "devanagari", False), ("OK 👍", "devanagari", False), (DEVA, None, False),
])
def test_a_reply_in_the_wrong_script_is_caught(reply, want, wrong):
    assert rl.mismatched(reply, want) is wrong


def _context(recent, **kw):
    base = dict(
        business_name="Aqirox", vertical="general", dna_summary=None, tone=None, policies=None, known_gaps=[],
        escalate_immediately=[], offerings={}, pricing_notes=None, opening_hours={}, availability=None,
        upcoming_bookings=None, cases=None, orders=None, properties=None, claims=None, open_shifts=None,
        customer_name="Nikhil", customer_summary=None, customer_since=None, identities=[], recent=recent, channel="web",
    )
    base.update(kw)
    return ctx.AgentContext(**base)


def _turns(*pairs):
    return [{"channel": "web", "direction": d, "text": t} for d, t in pairs]


def test_the_conversation_ends_with_the_line_about_the_last_message():
    c = _context(_turns(("inbound", "kal 1 baje"), ("outbound", "Theek hai sir"), ("inbound", "what services you offer?")))
    out = c.render_conversation()
    assert out.rstrip().endswith(rl.hint("what services you offer?"))
    c.language_retry = True
    assert rl.correction("latin") in c.render_conversation()
    assert "set by the business" in _context(_turns(("inbound", "hello")), reply_language="hi").render_conversation()


def _completion(message, cost=10):
    return SimpleNamespace(tool_input={"action": "reply", "message": message, "reasoning": "x", "confidence": 0.9},
                           cost_paise=cost, text="", model="m")


def test_a_whatsapp_draft_in_the_wrong_script_is_written_again(monkeypatch):
    prompts = []

    async def complete(**kw):
        prompts.append(kw["messages"][0]["content"][1]["text"])
        return _completion(DEVA if len(prompts) == 1 else "We build WhatsApp and Instagram automation.")

    monkeypatch.setattr(agent_module.client, "complete", complete)
    draft = asyncio.run(agent_module.draft_reply(_context(_turns(("inbound", "what services you offer?")))))
    assert draft.message == "We build WhatsApp and Instagram automation." and len(prompts) == 2
    assert rl.correction("latin") in prompts[1] and draft.cost_paise == 20


def test_a_right_script_draft_and_a_live_call_are_not_asked_again(monkeypatch):
    calls = []

    async def complete(**kw):
        calls.append(1)
        return _completion(DEVA)

    monkeypatch.setattr(agent_module.client, "complete", complete)
    asyncio.run(agent_module.draft_reply(_context(_turns(("inbound", "आपकी फीस कितनी है")))))
    asyncio.run(agent_module.draft_reply(_context(_turns(("inbound", "what is the fee?"))), fast=True))
    assert len(calls) == 2


def _stream(start, text):
    async def events():
        yield start
        yield agent_module.ReplyChunk(text=text)
    return events()


def test_a_web_reply_in_the_wrong_script_is_written_again(monkeypatch):
    seen = []

    def stream_reply(context):
        seen.append(context.language_retry)
        return _stream(agent_module.ReplyStart(action="reply"), "We build WhatsApp automation.")

    monkeypatch.setattr(agent_module, "stream_reply", stream_reply)
    c = _context(_turns(("inbound", "what services you offer?")))
    assert asyncio.run(web_reply._in_the_right_script(c, DEVA)) == "We build WhatsApp automation."
    assert seen == [True] and c.language_retry is False
    assert asyncio.run(web_reply._in_the_right_script(c, "We build apps.")) == "We build apps." and seen == [True]


def test_a_second_web_answer_that_would_book_is_not_taken(monkeypatch):
    monkeypatch.setattr(agent_module, "stream_reply", lambda c: _stream(
        agent_module.ReplyStart(action="reply", book_slot="2026-11-02T13:00:00+05:30"), "Booked for 1 pm."))
    c = _context(_turns(("inbound", "what services you offer?")))
    assert asyncio.run(web_reply._in_the_right_script(c, DEVA)) == DEVA
