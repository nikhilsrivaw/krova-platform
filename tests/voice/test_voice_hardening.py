"""
Voice pipeline behaviour that needs no phone, no Sarvam and no database: who counts
as "asking for a person", interrupting a reply, never repeating a promise, and ending
a dead line. Collaborators are scripted stand-ins.
"""

import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.channels.voice import pipeline as pl  # noqa: E402
from shared.channels.voice.pipeline import CallPipeline, Turn  # noqa: E402


def make(staff="919999900000"):
    spoken: list[str] = []
    state = SimpleNamespace(clears=0, ended=0)

    async def speak(chunks):
        async for text in chunks:
            spoken.append(text)
            yield b"x"

    async def send_audio(_):
        return None

    async def send_clear():
        state.clears += 1

    async def end_stream():
        state.ended += 1

    route = SimpleNamespace(
        business_id="b", staff_phone_number=staff, connection_id="c", greeting="hi",
    )
    pipe = CallPipeline(
        route=route, caller_phone="+91 90000 00000", provider_call_id="call-1",
        send_audio=send_audio, send_clear=send_clear, speak=speak, db=None,
        end_stream=end_stream,
    )
    return pipe, spoken, state


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    async def no_store(self, *, direction, text):
        return None

    monkeypatch.setattr(CallPipeline, "_store_turn", no_store)
    monkeypatch.setattr(pl, "_PLAYBACK_MARGIN_SECONDS", 0)


# ── who is asking for a person ──────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("contact the team", True),
    ("I want to contact your team please", True),
    ("team se baat karni hai", True),
    ("sales team ka contact number kya hai", False),
    ("what is the manager's email address", False),
    ("what is your team size", False),
    ("haircut ka rate kya hai", False),
])
def test_asking_for_a_person(text, expected):
    pipe, _, _ = make()
    pipe.turns.append(Turn(role="caller", text=text))
    assert pipe._caller_asked_for_person() is expected


@pytest.mark.parametrize("text,expected", [
    ("haan", True), ("hmm", True), ("okay ji", True), ("haan haan", True),
    ("haan mujhe appointment chahiye", False), ("what are your timings", False), ("", False),
])
def test_backchannel(text, expected):
    assert pl._is_backchannel(text) is expected


# ── never the same promise twice ────────────────────────────────────────────

def test_follow_up_promise_is_made_once_then_shortened():
    pipe, _, _ = make()
    first = pipe._followup_line("Sunday opening hours")
    second = pipe._followup_line("parking charges")
    third = pipe._followup_line(None)
    assert "follows up" in first and "Sunday opening hours" in first
    assert "follows up" not in second and "parking charges" in second
    assert "follows up" not in third


def test_staff_facing_reason_is_not_spoken():
    pipe, _, _ = make()
    line = pipe._followup_line("needs a person: refund")
    assert "needs a person" not in line and "refund" not in line


# ── interrupting ────────────────────────────────────────────────────────────

def test_barge_in_does_not_cancel_the_task_that_is_running_it():
    pipe, _, _ = make()

    async def go():
        async def inner():
            pipe._reply_task = asyncio.current_task()
            await pipe._barge_in()
            await asyncio.sleep(0)
            return "finished"

        return await asyncio.create_task(inner())

    assert asyncio.run(go()) == "finished"


def test_a_second_utterance_stops_the_first_reply_and_replies_never_overlap():
    pipe, _, _ = make()
    log: list[str] = []
    running = {"n": 0, "max": 0}

    async def slow_reply(self, *, started_at=None, speculation=None):
        text = self.turns[-1].text
        running["n"] += 1
        running["max"] = max(running["max"], running["n"])
        log.append(f"start {text}")
        try:
            await asyncio.sleep(0.2)
            log.append(f"done {text}")
        finally:
            running["n"] -= 1

    async def go():
        CallPipeline._reply = slow_reply
        first = asyncio.create_task(pipe.on_transcript("what are your timings", is_final=True))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(pipe.on_transcript("and the price", is_final=True))
        await asyncio.gather(first, second)

    original = CallPipeline._reply
    try:
        asyncio.run(go())
    finally:
        CallPipeline._reply = original

    assert running["max"] == 1
    assert "done what are your timings" not in log
    assert "done and the price" in log


def test_a_short_acknowledgement_does_not_interrupt_the_agent():
    pipe, _, state = make()

    async def go():
        async def talking():
            await asyncio.sleep(0.3)

        pipe._reply_task = asyncio.create_task(talking())
        await pipe.on_transcript("haan", is_final=False)
        interrupted = pipe._reply_task.cancelled() or pipe._reply_task.done()
        await pipe._reply_task
        return interrupted

    assert asyncio.run(go()) is False
    assert state.clears == 0


def test_keypad_zero_stops_the_current_reply_before_transferring():
    pipe, _, _ = make()
    calls: list[str] = []

    async def fake_transfer(self):
        calls.append("transfer")

    async def go():
        async def talking():
            await asyncio.sleep(5)

        old = asyncio.create_task(talking())
        pipe._reply_task = old
        original = CallPipeline.request_transfer
        CallPipeline.request_transfer = fake_transfer
        try:
            await pipe.on_dtmf("0")
            await pipe._reply_task
        finally:
            CallPipeline.request_transfer = original
        return old.cancelled()

    assert asyncio.run(go()) is True
    assert calls == ["transfer"]


# ── a dead line ends ────────────────────────────────────────────────────────

def test_a_silent_line_is_asked_once_then_ended(monkeypatch):
    monkeypatch.setattr(pl, "_WATCH_TICK", 0.01)
    monkeypatch.setattr(pl, "_IDLE_PROMPT_AFTER", 0.05)
    monkeypatch.setattr(pl, "_IDLE_HANGUP_AFTER", 0.05)
    pipe, spoken, state = make()

    asyncio.run(asyncio.wait_for(pipe.watch_idle(), timeout=3))

    assert spoken[0] == "Are you still there?"
    assert "end the call" in spoken[1]
    assert state.ended == 1


def test_a_caller_who_answers_the_check_keeps_the_call(monkeypatch):
    monkeypatch.setattr(pl, "_WATCH_TICK", 0.01)
    monkeypatch.setattr(pl, "_IDLE_PROMPT_AFTER", 0.05)
    monkeypatch.setattr(pl, "_IDLE_HANGUP_AFTER", 0.2)
    pipe, spoken, state = make()

    async def go():
        watcher = asyncio.create_task(pipe.watch_idle())
        await asyncio.sleep(0.12)
        await pipe.on_transcript("haan", is_final=False)
        await asyncio.sleep(0.1)
        watcher.cancel()

    asyncio.run(go())
    assert "Are you still there?" in spoken
    assert state.ended == 0


def test_a_call_over_the_time_limit_is_ended(monkeypatch):
    monkeypatch.setattr(pl, "_WATCH_TICK", 0.01)
    monkeypatch.setattr(pl, "_MAX_CALL_SECONDS", 0.03)
    pipe, spoken, state = make()

    asyncio.run(asyncio.wait_for(pipe.watch_idle(), timeout=3))

    assert "time limit" in spoken[0]
    assert state.ended == 1
