"""
Sarvam's realtime speech-to-text and streaming text-to-speech.

Chosen for one property that shapes the whole pipeline: every leg speaks
mu-law at 8kHz natively. Plivo sends it, Sarvam's STT accepts it directly,
Sarvam's TTS can emit it directly. No transcoding step exists anywhere
between a caller's voice and Claude's reply, which is where the latency
budget would otherwise go.

The other reason: `mode: codemix` and language auto-detection mean a caller
who mixes Hindi and English mid-sentence - which is how a large share of
Indian small-business calls actually sound - is read as one language rather
than needing to be split first.

Both clients are written against an injected websocket connection rather than
opening one internally, so the framing logic (what we send, how we parse what
comes back) is testable without a live Sarvam account.
"""

import base64
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

STT_URL = "wss://api.sarvam.ai/speech-to-text-realtime/ws"
TTS_URL = "wss://api.sarvam.ai/text-to-speech/ws"

STT_MODEL = "saaras:v3-realtime"
# v3, not v2: pronunciation dictionaries (needed for "Krova" and "Aqirox",
# both mispronounced unrecognisably by v2 on a real call) are v3-only.
TTS_MODEL = "bulbul:v3"

# Created once via POST /text-to-speech/pronunciation-dictionary (not at
# runtime - dictionaries are an account-level resource, not per-call).
# Passed as dict_id in every TTS config frame.
PRONUNCIATION_DICT_ID = "p_e5218216"

# Every leg of this call is mu-law at 8kHz - Plivo's native format - so this
# is the one encoding that requires no conversion anywhere in the pipeline.
AUDIO_ENCODING = "mulaw"
SAMPLE_RATE = 8000


class WSLike(Protocol):
    """The subset of a websocket connection this module needs, for testing."""

    async def send(self, data: str) -> None: ...
    async def recv(self) -> str: ...


def auth_headers() -> dict[str, str]:
    if not settings.sarvam_api_key:
        raise RuntimeError("SARVAM_API_KEY is not configured")
    return {"API-SUBSCRIPTION-KEY": settings.sarvam_api_key}


# ── speech to text ───────────────────────────────────────────────────────────

@dataclass(slots=True)
class Transcript:
    text: str
    is_final: bool
    language: str | None = None
    utterance_idx: int = 0


def stt_connect_url(*, language: str = "auto") -> str:
    """
    The realtime STT session is configured entirely by query parameters on
    the connect URL - confirmed against a real call, which a JSON config
    frame sent after connecting does not configure at all (the earlier,
    wrong assumption this replaced). There is no client-sent start frame:
    the server sends `session.begin` on its own once connected, and audio
    can start immediately after that.
    """
    params = {
        "language_code": language,
        "model": STT_MODEL,
        "stream_type": "balanced",
        # codemix reads Hindi/English mid-utterance as one language, matching
        # how a caller in this market actually talks rather than requiring
        # them to pick one.
        "mode": "codemix",
        "endpointing": "vad",
        "encoding": AUDIO_ENCODING,
        "sample_rate": str(SAMPLE_RATE),
    }
    return f"{STT_URL}?{urlencode(params)}"


def stt_audio_frame(mulaw_bytes: bytes) -> str:
    """One chunk of caller audio, as Sarvam's wire format expects it."""
    return json.dumps(
        {"event": "audio_input", "audio": base64.b64encode(mulaw_bytes).decode()}
    )


def parse_stt_event(raw: str) -> Transcript | None:
    """
    Read one message from Sarvam's STT socket.

    Returns None for anything that is not a transcript - the session.begin
    handshake, keepalives - so a caller can loop on results without filtering
    event types itself.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("unreadable STT frame")
        return None

    event = payload.get("event")
    if event == "vad.speech_end":
        logger.info("voice latency vad_speech_end=%.3f", time.monotonic())
    if event == "transcript.final":
        logger.info("voice latency transcript_final=%.3f", time.monotonic())
    if event not in ("transcript.partial", "transcript.final"):
        return None

    text = (payload.get("text") or "").strip()
    if not text:
        return None

    return Transcript(
        text=text,
        is_final=event == "transcript.final",
        language=payload.get("language"),
        utterance_idx=int(payload.get("utterance_idx", 0)),
    )


async def stream_transcripts(ws: WSLike) -> AsyncIterator[Transcript]:
    """Read transcripts from an already-connected, already-configured socket."""
    while True:
        raw = await ws.recv()
        transcript = parse_stt_event(raw)
        if transcript is not None:
            yield transcript


# ── text to speech ───────────────────────────────────────────────────────────

def tts_connect_url(*, model: str = TTS_MODEL) -> str:
    """
    Unlike STT, the rest of TTS config (speaker, language, codec) goes in a
    JSON frame after connecting - but `model` must be a query param on the
    connect URL itself, confirmed against a real call: omitting it makes the
    server hang the handshake indefinitely instead of rejecting it, which
    reads as a network problem rather than a missing parameter.

    send_completion_event is what makes one connection usable for a whole
    streamed reply, not just one string: sending text, then a flush, then
    more text, then another flush on the SAME connection - confirmed live
    against a real Sarvam connection - produces independent audio for each
    flush, each followed by its own `{"type":"event","data":{"event_type":
    "final"}}`. Without asking for that event there is no signal for when
    one sentence's audio has finished versus the next one's starting.
    """
    return f"{TTS_URL}?{urlencode({'model': model, 'send_completion_event': 'true'})}"


def is_tts_final_event(raw: str) -> bool:
    """True for the completion marker send_completion_event asks for - one per flush."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    return payload.get("type") == "event" and (payload.get("data") or {}).get(
        "event_type"
    ) == "final"


def tts_start_config(*, speaker: str = "shubh", language: str = "en-IN") -> str:
    """
    The first frame on a TTS connection.

    speaker defaults to "shubh" - v3's own default voice - because
    "anushka" (the earlier default) is a bulbul:v2 speaker and is not in
    v3's speaker list at all.

    min_buffer_size and max_chunk_length trade latency against choppiness:
    small values start speaking sooner but risk audible seams between
    independently-synthesised chunks. This was first tuned to 30/120 - well
    below the sarvamai SDK's own defaults (50/150) - guessing that a live
    caller cares most about not hearing dead air. A real call proved that
    wrong: speech was "not understandable," and Claude's own generation time
    (~2.5s, the dominant share of reply latency) makes the extra ~20
    characters of TTS buffering here immaterial to the total wait - so there
    was no real latency reason to have undercut Sarvam's own defaults in the
    first place. pace is left at Sarvam's default (omitted) for the same
    reason: 1.05 was never confirmed to help and is one more unverified knob
    on top of a clarity complaint.

    speech_sample_rate matters more than it looks: bulbul defaults to a
    much higher rate regardless of output_audio_codec, so mu-law audio
    generated here and declared to Plivo as 8000Hz (the field Plivo actually
    plays it at) came out garbled on a real call until this was made
    explicit.

    dict_id fixes a different real-call complaint entirely: "Krova" came
    out as something like "erova" and "Aqirox" was not recognisable as any
    word - proper nouns bulbul has never seen, mispronounced with total
    confidence rather than read out letter by letter. A pronunciation
    dictionary is the correct fix, not a phonetic respelling in the reply
    text itself, since Claude's own generated text has no way to know it
    needs one.
    """
    return json.dumps(
        {
            "type": "config",
            "data": {
                "speaker": speaker,
                "language_code": language,
                "min_buffer_size": 50,
                "max_chunk_length": 150,
                "output_audio_codec": AUDIO_ENCODING,
                "speech_sample_rate": SAMPLE_RATE,
                "dict_id": PRONUNCIATION_DICT_ID,
            },
        }
    )


def tts_text_frame(text: str) -> str:
    return json.dumps({"type": "text", "data": {"text": text}})


def tts_flush_frame() -> str:
    """Forces Sarvam to synthesise whatever text has been sent so far, immediately."""
    return json.dumps({"type": "flush"})


def parse_tts_event(raw: str) -> bytes | None:
    """Read one message from Sarvam's TTS socket. Returns raw mu-law bytes, or None."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    audio = ((payload.get("data") or {}).get("audio")) if isinstance(payload, dict) else None
    if not audio:
        return None

    try:
        return base64.b64decode(audio)
    except (ValueError, TypeError):
        logger.warning("unreadable TTS audio payload")
        return None


async def stream_audio(ws: WSLike) -> AsyncIterator[bytes]:
    """Read synthesised audio chunks from an already-connected, configured socket."""
    while True:
        raw = await ws.recv()
        chunk = parse_tts_event(raw)
        if chunk is not None:
            yield chunk
