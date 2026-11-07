"""The agent's voice is made louder before Plivo plays it, without clipping the peaks."""

import math
import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.channels.voice import loudness  # noqa: E402

FILLER = Path(__file__).resolve().parents[2] / "shared" / "channels" / "voice" / "assets" / "filler_en.ulaw"


def _rms_db(audio: bytes) -> float:
    speech = [loudness.decode(b) for b in audio if abs(loudness.decode(b)) > 300]
    return 20 * math.log10(math.sqrt(sum(s * s for s in speech) / len(speech)) / 32768)


def test_every_byte_survives_encode_and_decode():
    for b in range(256):
        again = loudness.encode(loudness.decode(b))
        assert loudness.decode(again) == loudness.decode(b)


def test_no_gain_changes_nothing():
    audio = bytes(range(256))
    assert loudness.Louder(0)(audio) == audio


def test_six_db_makes_real_speech_about_twice_as_loud():
    audio = FILLER.read_bytes()
    before, after = _rms_db(audio), _rms_db(loudness.Louder(6)(audio))
    assert 4.0 <= after - before <= 6.5


def test_loud_peaks_bend_instead_of_clipping():
    louder = loudness.table(12)
    peaks = [abs(loudness.decode(louder[b])) for b in range(256)]
    assert max(peaks) < 32635  # never pinned at the top
    order = sorted(range(128), key=lambda b: abs(loudness.decode(b)))
    out = [abs(loudness.decode(louder[b])) for b in order]
    assert out == sorted(out)  # louder input never comes out quieter


def test_the_gain_is_held_to_its_limits():
    assert loudness.Louder(40).gain_db == loudness.MAX_GAIN_DB and loudness.Louder(-5).gain_db == 0


def test_the_level_is_logged_once(caplog):
    caplog.set_level("INFO")
    louder = loudness.Louder(6)
    audio = FILLER.read_bytes()
    for _ in range(5):
        louder(audio)
    assert sum("voice level" in r.message for r in caplog.records) == 1
