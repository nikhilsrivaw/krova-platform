"""
The owner's own call and scripted campaign calls follow AI_LIVE_ROUTES like a customer call does, instead of always
going straight to Anthropic. No network: the provider and the router are replaced.
"""

import asyncio
from types import SimpleNamespace

from shared.ai import agent


def _collect(gen):
    async def run():
        return [e async for e in gen]
    return asyncio.run(run())


def _provider(text, seen):
    async def call(route, request, **_):
        seen["route"], seen["request"] = route, request
        return SimpleNamespace(text=text, tool_input=None, cost_paise=3,
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0))
    return call


def _routes(monkeypatch, mapping):
    monkeypatch.setattr(agent.router, "live_for", lambda task: mapping.get(task))


def _no_claude(monkeypatch):
    def boom(**kwargs):
        raise AssertionError("must not stream from Anthropic when a route is set")
    monkeypatch.setattr(agent.client, "stream_text", boom)


OWNER = SimpleNamespace(render=lambda: "Owed to you: Rs 5,000 by Sharma Traders.")
SCRIPTED = SimpleNamespace(render=lambda: "Ask the caller which day suits them.")
TURNS = [{"role": "user", "content": "kitna paisa baaki hai?"}]


def test_owner_call_uses_its_route(monkeypatch):
    seen: dict = {}
    _routes(monkeypatch, {"reply_owner_voice": "bedrock:mistral.mistral-large-3-675b-instruct"})
    _no_claude(monkeypatch)
    monkeypatch.setattr(agent.providers, "call", _provider("REPLY\n\nSharma Traders se Rs 5,000 baaki hai.", seen))
    events = _collect(agent.stream_owner_reply(OWNER, TURNS))
    assert seen["route"] == "bedrock:mistral.mistral-large-3-675b-instruct"
    roles = [m["role"] for m in seen["request"]["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert events[0].action == "reply"
    assert "5,000" in "".join(e.text for e in events if hasattr(e, "text"))
    assert events[-1].cost_paise == 3


def test_scripted_call_uses_its_route(monkeypatch):
    seen: dict = {}
    _routes(monkeypatch, {"reply_scripted_voice": "bedrock:moonshotai.kimi-k2.5"})
    _no_claude(monkeypatch)
    monkeypatch.setattr(agent.providers, "call", _provider("REPLY\n\nAapko kaunsa din theek rahega?", seen))
    events = _collect(agent.stream_scripted_reply(SCRIPTED, [{"role": "user", "content": "haan boliye"}]))
    assert seen["route"] == "bedrock:moonshotai.kimi-k2.5"
    assert events[0].action == "reply"


def test_a_claim_the_context_never_made_is_not_spoken(monkeypatch):
    # the voice guard: "paid" is a claim word, and nothing in the owner's ledger says anything was paid
    _routes(monkeypatch, {"reply_owner_voice": "bedrock:moonshotai.kimi-k2.5"})
    _no_claude(monkeypatch)
    monkeypatch.setattr(agent.providers, "call", _provider("REPLY\n\nSharma Traders ne paid kar diya hai.", {}))
    events = _collect(agent.stream_owner_reply(OWNER, TURNS))
    spoken = "".join(e.text for e in events if hasattr(e, "text"))
    assert "paid" not in spoken and spoken


def test_no_route_still_streams_from_claude(monkeypatch):
    _routes(monkeypatch, {})
    called = {}

    class Stream:
        cost_paise = 9

        def __aiter__(self):
            async def gen():
                yield "NOACTION\n"
            return gen()

    def stream_text(**kwargs):
        called.update(kwargs)
        return Stream()

    monkeypatch.setattr(agent.client, "stream_text", stream_text)
    events = _collect(agent.stream_owner_reply(OWNER, TURNS))
    assert called["task"] == "reply_owner_voice"
    assert events[0].action == "no_action" and events[-1].cost_paise == 9


def test_converse_route_sends_the_turns_to_claude_on_bedrock(monkeypatch):
    seen: dict = {}
    _routes(monkeypatch, {"reply_scripted_voice": "bedrock-converse:in.anthropic.claude-haiku-4-5-20251001-v1:0"})
    _no_claude(monkeypatch)

    async def converse(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(text="REPLY\n\nTheek hai.", cost_paise=2)

    monkeypatch.setattr(agent.bedrock_claude, "converse", converse)
    events = _collect(agent.stream_scripted_reply(SCRIPTED, [{"role": "user", "content": "haan boliye"}]))
    assert seen["model_id"] == "in.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert "Caller: haan boliye" in seen["volatile"]
    assert events[0].action == "reply"
