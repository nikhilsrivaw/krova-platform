"""
Makes the agent's voice louder on a call, before it goes to Plivo.

Callers had to press the phone to their ear to hear it. Sarvam's bulbul:v3 ignores its own `loudness` setting
(docs.sarvam.ai, text-to-speech stream: "NOT supported for bulbul:v3"), so the gain is applied here. The filler clip
Sarvam made measured about -18 dBFS of speech, quiet for a phone line.

Audio arrives as 8-bit mu-law, so every byte maps to one louder byte: the whole change is one 256-entry table and
bytes.translate, with no dependency and no cost per call worth measuring. Above half of full scale the table bends
the signal down smoothly instead of clipping it, so louder never means harsher on the peaks.
"""

import math

from shared.utils.logging import get_logger

logger = get_logger(__name__)

MAX_GAIN_DB = 12.0
# Where the smooth bend starts, as a share of full scale (-6 dBFS).
_KNEE = 0.5
_BIAS = 0x84
_CLIP = 32635


def decode(byte: int) -> int:
    """One G.711 mu-law byte as a 16-bit sample."""
    byte = ~byte & 0xFF
    exponent, mantissa = (byte >> 4) & 0x07, byte & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if byte & 0x80 else sample


def encode(sample: int) -> int:
    """One 16-bit sample as a G.711 mu-law byte."""
    sign = 0x80 if sample < 0 else 0
    sample = min(abs(sample), _CLIP) + _BIAS
    exponent, mask = 7, 0x4000
    while exponent > 0 and not sample & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _limited(value: float) -> float:
    """value in -1..1 after gain may pass 1; above the knee it bends smoothly towards 1 and never reaches it."""
    size = abs(value)
    if size <= _KNEE:
        return value
    bent = _KNEE + (1 - _KNEE) * math.tanh((size - _KNEE) / (1 - _KNEE))
    return math.copysign(bent, value)


def table(gain_db: float) -> bytes:
    """The byte-for-byte map that makes mu-law audio `gain_db` louder."""
    factor = 10 ** (max(0.0, min(MAX_GAIN_DB, gain_db)) / 20)
    return bytes(encode(round(_limited(decode(b) * factor / 32768) * 32768)) for b in range(256))


class Louder:
    """Applies the gain, and logs once per call how loud the audio arrived, so the setting can be checked on real calls."""

    REPORT_AFTER_SAMPLES = 8000 * 4  # four seconds of speech
    _SPEECH = 300  # quieter than this is the gap between words

    def __init__(self, gain_db: float, call_id: str | None = None) -> None:
        self.gain_db = max(0.0, min(MAX_GAIN_DB, gain_db))
        self._table = table(self.gain_db) if self.gain_db else None
        self._call_id = call_id
        self._squares = 0.0
        self._samples = 0
        self._peak = 0
        self._reported = False

    def __call__(self, audio: bytes) -> bytes:
        if not self._reported:
            self._measure(audio)
        return audio.translate(self._table) if self._table else audio

    def _measure(self, audio: bytes) -> None:
        for b in audio:
            sample = abs(decode(b))
            self._peak = max(self._peak, sample)
            if sample > self._SPEECH:
                self._squares += sample * sample
                self._samples += 1
        if self._samples >= self.REPORT_AFTER_SAMPLES:
            self._reported = True
            rms = math.sqrt(self._squares / self._samples)
            logger.info(
                "voice level call=%s speech_rms=%.1fdBFS peak=%.1fdBFS before gain, gain=%.1fdB",
                self._call_id, 20 * math.log10(rms / 32768), 20 * math.log10(max(1, self._peak) / 32768), self.gain_db,
            )
