"""
A caller asking for a person is put through however the speech-to-text wrote it: Sarvam's codemix writes Hindi in
Devanagari ("मुझे टीम से बात करनी है"), which the English-letters check missed - so nothing transferred. And the
model can say PERSON for wordings no list catches.
"""

import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.ai import agent  # noqa: E402
from shared.ai import context as ctx  # noqa: E402
from shared.channels.voice import pipeline as pl  # noqa: E402
from shared.channels.voice.pipeline import CallPipeline, Turn  # noqa: E402
from shared.team.requests import PERSON_REQUEST  # noqa: E402


@pytest.mark.parametrize("said", [
    "मुझे टीम से बात करनी है", "मुझे team से बात करनी है", "किसी इंसान से बात कराओ", "manager से connect करो",
    "मालिक से बात करवा दीजिए", "किसी से बात हो सकती है क्या", "कॉल ट्रांसफर कर दो", "मुझे इंसान चाहिए",
    "team se baat karni hai", "I want to talk to a team member",
])
def test_a_request_for_a_person_is_heard_in_devanagari_and_mixed(said):
    assert PERSON_REQUEST.search(said)


@pytest.mark.parametrize("said", [
    "आपकी टीम कितनी बड़ी है", "मुझे कोई discount चाहिए", "कोई offer है क्या", "मैनेजर का नाम क्या है",
    "team size kitna hai", "price kya hai",
])
def test_a_question_about_the_team_is_not_a_request(said):
    assert not PERSON_REQUEST.search(said)


def _context():
    return ctx.AgentContext(
        business_name="Shop", vertical="general", dna_summary=None, tone=None, policies=None, known_gaps=[],
        escalate_immediately=[], offerings={}, pricing_notes=None, opening_hours={}, availability=None,
        upcoming_bookings=None, cases=None, orders=None, properties=None, claims=None, open_shifts=None,
        customer_name="Ravi", customer_summary=None, customer_since=None, identities=[],
        recent=[{"channel": "voice", "direction": "inbound", "text": "kisi asli aadmi se baat karao"}], channel="voice",
    )


@pytest.mark.parametrize("output", ["PERSON\n", "PERSON"])
def test_the_model_can_say_person(monkeypatch, output):
    monkeypatch.setattr(agent.router, "live_for", lambda task: None)

    class Stream:
        cost_paise = 1

        def __aiter__(self):
            async def gen():
                yield output
            return gen()

    monkeypatch.setattr(agent.client, "stream_text", lambda **kw: Stream())

    async def collect():
        return [e async for e in agent.stream_reply(_context())]

    events = asyncio.run(collect())
    assert events[0].action == "escalate" and events[0].wants_person
    assert "PERSON when the caller asks to speak to a person" in agent.SYSTEM_STREAM


def test_a_call_is_put_through_when_the_model_says_person(monkeypatch):
    async def no_store(self, *, direction, text):
        return None

    monkeypatch.setattr(CallPipeline, "_store_turn", no_store)

    async def speak(chunks):
        async for _ in chunks:
            yield b"x"

    async def nothing(*a, **k):
        return None

    route = SimpleNamespace(business_id="b", staff_phone_number="919999900000", connection_id="c", greeting="hi",
                            keypad_menu=None, language="hi-IN", extra={})
    pipe = CallPipeline(route=route, caller_phone="+91 90000 00000", provider_call_id="call-1", send_audio=nothing,
                        send_clear=nothing, speak=speak, db=None, end_stream=nothing)
    pipe.customer_id = "c1"
    pipe.turns.append(Turn(role="caller", text="kisi asli aadmi se baat karao"))
    done = []

    async def events(_context):
        yield agent.ReplyStart(action="escalate", wants_person=True)

    async def call_context(self):
        return None

    async def targets(self):
        return ["member"]

    async def transfer(self, **kw):
        done.append(kw["targets"])

    monkeypatch.setattr(pl.agent_module, "stream_reply", events)
    monkeypatch.setattr(CallPipeline, "_call_context", call_context)
    monkeypatch.setattr(CallPipeline, "_transfer_targets", targets)
    monkeypatch.setattr(CallPipeline, "request_transfer", transfer)
    monkeypatch.setattr(CallPipeline, "_caller_asked_for_person", lambda self: False)
    asyncio.run(pipe._reply())
    assert done == [["member"]]
