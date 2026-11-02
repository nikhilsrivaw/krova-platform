"""
Barge-in goes by what the caller can still hear, not by whether the reply task is running: the reply is generated and
handed to Plivo far faster than it plays, so "talking over the agent" mostly happens after the task has finished.
"""

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.channels.voice import pipeline as pl  # noqa: E402
from shared.channels.voice.pipeline import CallPipeline, Turn, _is_backchannel  # noqa: E402


def make(opening_line=None):
    spoken, state = [], SimpleNamespace(clears=0)

    async def speak(chunks):
        async for text in chunks:
            spoken.append(text)
            yield b"x" * 8000 * 4  # four seconds of audio, sent at once

    async def nothing(*a, **k):
        return None

    async def send_clear():
        state.clears += 1

    route = SimpleNamespace(business_id="b", staff_phone_number="919999900000", connection_id="c", greeting="hi",
                            keypad_menu=None, language="hi-IN", extra={})
    pipe = CallPipeline(route=route, caller_phone="+91 90000 00000", provider_call_id="call-1", send_audio=nothing,
                        send_clear=send_clear, speak=speak, db=None, end_stream=nothing, opening_line=opening_line)
    return pipe, spoken, state


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    async def no_store(self, *, direction, text):
        return None

    monkeypatch.setattr(CallPipeline, "_store_turn", no_store)


def test_talking_over_audio_that_is_still_playing_stops_it():
    pipe, _, state = make()

    async def go():
        await pipe._say_stream(pl._single_chunk("Hamare yahan haircut 300 rupaye ka hai aur facial 800 ka."), record=True)
        assert pipe._reply_task is None and pipe._audible()  # the task is over, the caller still hears it
        pipe._reply_audio_start = time.monotonic() - 2  # two of its four seconds have played
        await pipe.on_transcript("ruko ek minute", is_final=False)

    asyncio.run(go())
    assert state.clears == 1 and not pipe._audible()
    agent = [t for t in pipe.turns if t.role == "agent"][-1]
    assert len(agent.text) < len("Hamare yahan haircut 300 rupaye ka hai aur facial 800 ka.")  # only what was heard


def test_once_the_audio_has_played_nothing_is_cleared():
    pipe, _, state = make()

    async def go():
        await pipe._say_stream(pl._single_chunk("Ji boliye."), record=True)
        pipe._playing_until = time.monotonic() - 1
        await pipe.on_transcript("price kya hai", is_final=False)

    asyncio.run(go())
    assert state.clears == 0


@pytest.mark.parametrize("said, stops", [
    ("हाँ", False), ("हाँ जी", False), ("ठीक है", False), ("theek hai", False), ("haan ji bilkul", False),
    ("ruko", True), ("nahi", True), ("ek minute", True), ("suniye", True), ("haan par price kya hai", True),
])
def test_a_listeners_haan_never_stops_the_agent_but_ruko_does(said, stops):
    assert _is_backchannel(said) is not stops
    pipe, _, state = make()

    async def go():
        await pipe._say_stream(pl._single_chunk("Aapka appointment kal subah das baje ka hai."), record=True)
        await pipe.on_transcript(said, is_final=False)

    asyncio.run(go())
    assert (state.clears == 1) is stops


def test_the_agents_own_voice_coming_back_is_not_an_interruption():
    pipe, _, state = make()

    async def go():
        await pipe._say_stream(pl._single_chunk("Aapka appointment kal subah das baje ka hai."), record=True)
        await pipe.on_transcript("appointment kal subah das baje", is_final=False)

    asyncio.run(go())
    assert state.clears == 0


def test_a_sentence_said_over_the_opening_line_is_answered_after_it(monkeypatch):
    pipe, _, state = make(opening_line="Namaste, main Sharma Clinic se bol rahi hoon, aapki report aa gayi hai.")
    answered = []

    async def handle(self, text):
        answered.append(text)

    async def speak(chunks):
        async for _ in chunks:
            # the person talks while the line is playing
            await pipe.on_transcript("hello?", is_final=True)  # picking up - the line itself answers that
            await pipe.on_transcript("abhi busy hoon, baad mein call karo", is_final=True)
            yield b"x" * 800

    monkeypatch.setattr(CallPipeline, "_handle_utterance", handle)
    pipe.speak = speak
    asyncio.run(pipe.start())
    assert answered == ["abhi busy hoon, baad mein call karo"] and state.clears == 0  # the line was not cut


def test_nothing_heard_yet_is_recorded_as_such():
    pipe, _, _ = make()

    async def go():
        await pipe._say_stream(pl._single_chunk("Aapki booking ho gayi hai."), record=True)
        await pipe.on_transcript("ruko ruko", is_final=False)

    asyncio.run(go())
    assert [t for t in pipe.turns if t.role == "agent"][-1].text == "(interrupted before it was heard)"


def test_the_quiet_timer_counts_from_when_the_agent_stops_being_heard():
    pipe, _, _ = make()
    asyncio.run(pipe._say_stream(pl._single_chunk("Ek lambi baat."), record=True))
    assert pipe._last_activity >= pipe._playing_until - 0.01 > time.monotonic()


# ── the risks closed before shipping ────────────────────────────────────────

def test_haan_over_the_end_of_a_question_is_taken_as_the_answer(monkeypatch):
    pipe, _, state = make()
    answered = []

    async def handle(self, text):
        answered.append(text)

    monkeypatch.setattr(CallPipeline, "_handle_utterance", handle)

    async def go():
        await pipe._say_stream(pl._single_chunk("Kal 4 baje wali booking cancel kar doon?"), record=True)
        pipe._playing_until = time.monotonic() + 0.05  # the question is just finishing
        await pipe.on_transcript("haan ji", is_final=True)

    asyncio.run(go())
    assert answered == ["haan ji"] and state.clears == 0  # waited for the question to end, did not cut it


def test_haan_while_the_agent_is_just_talking_is_still_ignored(monkeypatch):
    pipe, _, _ = make()
    answered = []

    async def handle(self, text):
        answered.append(text)

    monkeypatch.setattr(CallPipeline, "_handle_utterance", handle)

    async def go():
        await pipe._say_stream(pl._single_chunk("Hamare yahan haircut 300 ka hai."), record=True)
        await pipe.on_transcript("haan", is_final=True)

    asyncio.run(go())
    assert answered == []


def test_a_real_answer_that_shares_words_with_the_agent_is_not_an_echo(monkeypatch):
    pipe, _, state = make()
    answered = []

    async def handle(self, text):
        answered.append(text)

    monkeypatch.setattr(CallPipeline, "_handle_utterance", handle)

    async def go():
        await pipe._say_stream(pl._single_chunk("Kal 4 baje ka slot khali hai."), record=True)
        await pipe.on_transcript("kal 4 baje nahi", is_final=False)
        await pipe.on_transcript("kal 4 baje nahi, 6 baje", is_final=True)

    asyncio.run(go())
    assert state.clears == 1 and answered == ["kal 4 baje nahi, 6 baje"]


def test_one_stray_word_of_noise_does_not_cut_the_agent_off():
    pipe, _, state = make()

    async def go():
        await pipe._say_stream(pl._single_chunk("Aapki report kal tak aa jayegi."), record=True)
        await pipe.on_transcript("the", is_final=False)
        assert state.clears == 0
        await pipe.on_transcript("ruko", is_final=False)  # one word, but it means stop

    asyncio.run(go())
    assert state.clears == 1


def test_haan_near_the_end_of_an_opening_question_is_kept(monkeypatch):
    pipe, _, _ = make(opening_line="Namaste, aapki report aa gayi hai. Kya aap kal aa sakte hain?")
    answered = []

    async def handle(self, text):
        answered.append(text)

    async def speak(chunks):
        async for _ in chunks:
            yield b"x" * 800
        # the line has been sent and mostly played when they answer
        pipe._reply_audio_start = time.monotonic() - 10
        await pipe.on_transcript("haan", is_final=True)

    monkeypatch.setattr(CallPipeline, "_handle_utterance", handle)
    pipe.speak = speak
    asyncio.run(pipe.start())
    assert answered == ["haan"]
