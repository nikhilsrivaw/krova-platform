"""An Urdu caller is heard by speech-to-text but the voice cannot speak Urdu: answer in the Hindi voice, in Devanagari."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest  # noqa: E402

from shared.ai import hand_over_lines, reply_language  # noqa: E402
from shared.channels.voice import sarvam  # noqa: E402
from shared.team import language as team_language  # noqa: E402


@pytest.mark.parametrize("heard, spoken", [
    ("ur-IN", "hi-IN"), ("ne-IN", "hi-IN"), ("kok-IN", "hi-IN"), ("mai-IN", "hi-IN"), ("as-IN", "bn-IN"),
    ("hi-IN", "hi-IN"), ("ta-IN", "ta-IN"), ("od-IN", "od-IN"), ("en-IN", "en-IN"),
])
def test_the_voice_is_only_ever_asked_for_a_language_it_speaks(heard, spoken):
    assert sarvam.speakable(heard) == spoken
    assert sarvam.speakable(heard) in sarvam.TTS_LANGUAGES


def test_an_unknown_code_falls_back_to_the_lines_own_language():
    assert sarvam.speakable("xx-YY", fallback="ta-IN") == "ta-IN"
    assert sarvam.speakable(None, fallback="hi-IN") == "hi-IN"
    assert sarvam.speakable("xx-YY", fallback="ur-IN") == "en-IN"


def test_an_urdu_callers_hand_over_line_is_said_in_devanagari():
    said_in = team_language.from_voice("ur-IN")
    assert said_in == "hindi"
    line = hand_over_lines.told(said_in)
    assert line and any(0x0900 <= ord(c) <= 0x097F for c in line)


def test_urdu_script_from_speech_asks_for_a_devanagari_reply():
    hint = reply_language.spoken_hint("فیس کتنی ہے؟ داخلہ کب تک ہے")
    assert hint and "Devanagari" in hint and "Urdu" in hint


@pytest.mark.parametrize("heard", ["फीस कितनी है", "fees kitni hai", "What is the fee?", "", None])
def test_other_speech_keeps_the_usual_language_rule(heard):
    assert reply_language.spoken_hint(heard) is None


def test_the_spoken_rule_is_only_for_calls():
    from types import SimpleNamespace

    from shared.ai.context import AgentContext

    urdu = "فیس کتنی ہے؟ داخلہ کب تک ہے"
    for channel, expect_spoken in (("voice", True), ("whatsapp", False)):
        ctx = SimpleNamespace(channel=channel, last_inbound=lambda: urdu, reply_language="auto", picked=None,
                              language_retry=False)
        line = AgentContext.language_line(ctx)
        assert bool(line and "Devanagari" in line) is expect_spoken
