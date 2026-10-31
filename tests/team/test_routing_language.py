"""Routing by the language a customer writes in: scripts read exactly, English vs Hinglish guessed from common words."""

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.team import language as lang  # noqa: E402
from shared.team import rules  # noqa: E402


@pytest.mark.parametrize("text, expected", [
    ("Hello, I would like to know the price of the premium plan.", "english"),
    ("Can you send me the details?", "english"),
    ("ok thanks", "english"),
    ("bhai price kya hai", "hinglish"),
    ("Mera order abhi tak nahi aaya", "hinglish"),
    ("kal aa jaunga", "hinglish"),
    ("मेरा ऑर्डर कब आएगा", "hindi"),
    ("என் ஆர்டர் எங்கே", "tamil"),
    ("আমার অর্ডার", "bengali"),
    ("ਮੇਰਾ ਆਰਡਰ order", "punjabi"),
    ("", None),
    ("123 !!", None),
])
def test_detect(text, expected):
    assert lang.detect(text) == expected


def test_a_calls_language_comes_from_speech():
    assert lang.from_voice("hi-IN") == "hindi" and lang.from_voice("en-IN") == "english"
    assert lang.from_voice("ta-IN") == "tamil" and lang.from_voice(None) is None and lang.from_voice("xx-YY") is None


def test_a_language_rule_matches_on_what_the_customer_wrote():
    c = {"languages": ["english"]}
    assert rules.matches(c, text="Could you share the brochure please?", channel="whatsapp", connection_id=None,
                         lead_source=None, tags=set())
    assert not rules.matches(c, text="brochure bhej do na bhai", channel="whatsapp", connection_id=None,
                             lead_source=None, tags=set())


def test_a_known_language_wins_over_the_message_text():
    # On a call the transcript may be short; the language heard on the call decides.
    assert rules.matches({"languages": ["tamil"]}, text="ok", channel="voice", connection_id=None, lead_source=None,
                         tags=set(), language="tamil")


def test_unknown_languages_are_dropped_when_saving():
    assert rules.clean_conditions({"languages": ["english", "klingon", "tamil"]}) == {"languages": ["english", "tamil"]}
