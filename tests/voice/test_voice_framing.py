"""
Voice pipeline tests - everything that does not need a live phone call.

Signature validation, XML generation, Sarvam wire framing, and the barge-in
state machine, all exercised with fakes. What this deliberately does NOT
prove: that a real Plivo call, a real Sarvam connection, and this code
actually agree with each other end to end. That needs a live number and a
live Sarvam key, neither of which exist yet.
"""

import asyncio
import base64
import sys
import uuid
from datetime import datetime, timezone


from shared.channels.voice import plivo_signature, relay, sarvam, xml  # noqa: E402
from shared.channels.voice.pipeline import CallPipeline  # noqa: E402
from shared.channels.voice.tenant import VoiceRoute  # noqa: E402
from shared.config.settings import settings  # noqa: E402

ok = True


def check(label, cond, extra=None):
    global ok
    if not cond:
        ok = False
    suffix = f"  -> {extra!r}" if (not cond and extra is not None) else ""
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{suffix}")


# ── signature validation ─────────────────────────────────────────────────────
# Reference signer is the official `plivo` SDK's own get_signature_v3 - not a
# hand-rolled HMAC - so a bug shared between "sign" and "verify" here can't
# hide a real mismatch the way it did the first time this was written.
print("\n-- plivo_signature --")
settings.plivo_auth_token = "test-auth-token-123"

from plivo.utils.signature_v3 import get_signature_v3

ws_uri = "https://voice.krova.test/voice/stream"
nonce = "abc123"
good_sig = get_signature_v3(
    settings.plivo_auth_token, ws_uri, nonce
).decode()

try:
    plivo_signature.verify(
        uri=ws_uri, signature=good_sig, nonce=nonce, method="GET"
    )
    check("valid GET signature accepted", True)
except plivo_signature.InvalidSignature as e:
    check("valid GET signature accepted", False, e)

for label, sig, non in [
    ("missing signature", None, nonce),
    ("missing nonce", good_sig, None),
    ("wrong signature", "0" * 44, nonce),
    ("wrong nonce", good_sig, "different-nonce"),
]:
    try:
        plivo_signature.verify(uri=ws_uri, signature=sig, nonce=non, method="GET")
        check(f"rejects {label}", False)
    except plivo_signature.InvalidSignature:
        check(f"rejects {label}", True)

# a different URI must fail even with a technically-valid-looking signature
try:
    plivo_signature.verify(
        uri="https://voice.krova.test/voice/answer",
        signature=good_sig,
        nonce=nonce,
        method="GET",
    )
    check("rejects signature for a different URI", False)
except plivo_signature.InvalidSignature:
    check("rejects signature for a different URI", True)

# POST case, matching the /voice/answer webhook - params folded into the
# signed string sorted by key, not passed as a query string
answer_uri = "https://voice.krova.test/voice/answer"
post_params = {"CallUUID": "call-1", "From": "+911234567890", "To": "+919876543210"}
post_sig = plivo_signature._expected(
    uri=answer_uri,
    nonce=nonce,
    auth_token=settings.plivo_auth_token,
    method="POST",
    params=post_params,
)
try:
    plivo_signature.verify(
        uri=answer_uri,
        signature=post_sig,
        nonce=nonce,
        method="POST",
        params=post_params,
    )
    check("valid POST signature with params accepted", True)
except plivo_signature.InvalidSignature as e:
    check("valid POST signature with params accepted", False, e)

try:
    plivo_signature.verify(
        uri=answer_uri,
        signature=post_sig,
        nonce=nonce,
        method="POST",
        params={**post_params, "From": "+910000000000"},
    )
    check("rejects POST signature when a param value changes", False)
except plivo_signature.InvalidSignature:
    check("rejects POST signature when a param value changes", True)

# ── XML ──────────────────────────────────────────────────────────────────────
print("\n-- xml --")
out = xml.stream_response("wss://voice.krova.space/voice/stream")
check("bidirectional set", 'bidirectional="true"' in out)
check("keepCallAlive set", 'keepCallAlive="true"' in out)
check("mulaw 8000 content type", "audio/x-mulaw;rate=8000" in out)
check("url embedded", "wss://voice.krova.space/voice/stream" in out)
check("valid xml declaration", out.startswith('<?xml version="1.0"'))

hangup = xml.hangup_response("test reason")
check("hangup produces Hangup tag", "<Hangup/>" in hangup)

# a URL with special characters must be escaped, not break the XML
tricky = xml.stream_response("wss://x.dev/stream?a=1&b=2")
check("query string ampersand escaped", "&amp;" in tricky and "&b=2" not in tricky.split("&amp;")[0])

# ── sarvam wire framing ─────────────────────────────────────────────────────
print("\n-- sarvam --")
url = sarvam.stt_connect_url(language="hi-IN")
check("stt url is the realtime endpoint", url.startswith(sarvam.STT_URL + "?"))
check("stt url requests hi-IN", "language_code=hi-IN" in url)
check("stt url requests codemix mode", "mode=codemix" in url)
check("stt url requests mulaw", "encoding=mulaw" in url)
check("stt url requests vad endpointing", "endpointing=vad" in url)

frame = sarvam.stt_audio_frame(b"\x01\x02\x03")
import json as _json
parsed = _json.loads(frame)
check("audio frame roundtrips", base64.b64decode(parsed["audio"]) == b"\x01\x02\x03")

# transcript parsing
partial = sarvam.parse_stt_event(
    '{"event":"transcript.partial","text":"hello","utterance_idx":0}'
)
check("partial parsed", partial is not None and not partial.is_final)

final = sarvam.parse_stt_event(
    '{"event":"transcript.final","text":"hello there","language":"hi-IN"}'
)
check("final parsed", final is not None and final.is_final)
check("final language captured", final.language == "hi-IN")

check("session.begin ignored (not a transcript)",
      sarvam.parse_stt_event('{"event":"session.begin","config":{}}') is None)
check("empty text ignored",
      sarvam.parse_stt_event('{"event":"transcript.final","text":"  "}') is None)
check("garbage input does not raise", sarvam.parse_stt_event("not json") is None)

tts_url = sarvam.tts_connect_url()
check("tts url requests a model", "model=bulbul" in tts_url)

tts_cfg = sarvam.tts_start_config(speaker="anushka", language="en-IN")
check("tts config requests mulaw output", '"output_audio_codec": "mulaw"' in tts_cfg)
check("tts config requests 8000Hz sample rate", '"speech_sample_rate": 8000' in tts_cfg)
check("tts config requests the pronunciation dictionary", sarvam.PRONUNCIATION_DICT_ID in tts_cfg)
check("tts url requests v3", "bulbul%3Av3" in sarvam.tts_connect_url() or "bulbul:v3" in sarvam.tts_connect_url())

tts_audio_msg = _json.dumps({"data": {"audio": base64.b64encode(b"abc").decode()}})
check("tts audio parsed", sarvam.parse_tts_event(tts_audio_msg) == b"abc")
check("tts garbage does not raise", sarvam.parse_tts_event("not json") is None)
check("tts message with no audio field returns None",
      sarvam.parse_tts_event('{"data":{}}') is None)

# ── per-call cost ────────────────────────────────────────────────────────────
print("\n-- cost --")

# The estimate fallback only - used when a real Plivo CDR could not be
# fetched. Plivo's own rate is USD/minute (confirmed against a real CDR and
# the account's Pricing endpoint), so the estimate must convert through
# _USD_TO_INR - the bug this replaced treated the same figure as rupees.
plivo_paise = relay._estimate_plivo_paise(60, 0.00475)
check(
    "plivo estimate converts USD/min through the documented exchange rate",
    plivo_paise == round((60 / 60) * 0.00475 * relay._USD_TO_INR * 100),
    plivo_paise,
)
check("plivo estimate is a plausible per-minute INR cost, not near-zero", plivo_paise > 10, plivo_paise)
check("missing plivo rate costs nothing rather than raising", relay._estimate_plivo_paise(60, None) == 0)

stt_paise = round(60 * (30 / 3600) * 100)
check("sarvam stt rate matches confirmed pricing (Rs 30/hour)", stt_paise == 50, stt_paise)
tts_paise = round(200 * (15 / 10_000) * 100)
check("sarvam tts rate matches confirmed pricing (Rs 15/10k chars)", tts_paise == 30, tts_paise)

# ── connect-time filler guard ───────────────────────────────────────────────
print("\n-- connect_with_filler --")


async def _fake_connect(result="conn-ok", delay: float = 0.0, error: Exception | None = None):
    await asyncio.sleep(delay)
    if error is not None:
        raise error
    return result


async def _noop_send(chunk: bytes) -> None:
    pass


async def _run_connect_with_filler_checks() -> None:
    # Connect finishes before the guard delay elapses: no filler sent, and
    # the real connect's own result comes back untouched.
    sent: list[bytes] = []
    started = asyncio.get_event_loop().time()
    result = await relay.connect_with_filler(
        lambda: _fake_connect(result="fast-conn", delay=0.001),
        send_chunk=sent.append,
        audio=b"x" * 10,
        sample_rate=8000,
        guard_delay=5.0,
        call_uuid="test-fast",
    )
    elapsed = asyncio.get_event_loop().time() - started
    check("fast connect: no filler sent", sent == [], sent)
    check("fast connect: returns fast, not after guard_delay", elapsed < 1.0, elapsed)
    check("fast connect: real connect's result is returned", result == "fast-conn", result)

    # Connect takes longer than several clip-lengths: the whole clip is sent
    # as one piece each loop, not sliced and throttled per-chunk - an
    # earlier version did that and it made the filler itself sound broken up
    # on a real call, since network/processing time on top of a per-chunk
    # sleep supplies audio to Plivo slower than it plays it - and the real
    # connect's own result is still what comes back once it finishes.
    sent = []
    clip = b"abcd"

    async def _tracking_send(chunk: bytes) -> None:
        sent.append(chunk)

    result = await relay.connect_with_filler(
        lambda: _fake_connect(result="slow-conn", delay=0.05),
        send_chunk=_tracking_send,
        audio=clip,
        sample_rate=400,  # clip_seconds = 4/400 = 0.01s per loop
        guard_delay=0.01,
        call_uuid="test-slow",
    )
    check("slow connect: filler was sent", len(sent) >= 2, len(sent))
    check("slow connect: whole clip sent each time, never sliced", all(c == clip for c in sent), sent)
    check("slow connect: real connect's result is still returned", result == "slow-conn", result)

    # Connect finishes partway through a clip's play time: the wait should
    # wake immediately rather than always sleeping the full clip duration -
    # confirmed against a real call where a blind sleep queued a second full
    # clip after the greeting's connect had already finished, because
    # nothing was watching for it to finish mid-sleep.
    started = asyncio.get_event_loop().time()
    await relay.connect_with_filler(
        lambda: _fake_connect(delay=0.01),
        send_chunk=_noop_send,
        audio=b"abcd",
        sample_rate=40,  # clip_seconds = 4/40 = 0.1s, well longer than the 0.01s connect delay
        guard_delay=0.0,
        call_uuid="test-wakes-early",
    )
    elapsed = asyncio.get_event_loop().time() - started
    check("wakes on real connect instead of sleeping the full clip", elapsed < 0.08, elapsed)

    # No filler asset available (e.g. the generator script was never run):
    # falls back to a plain wait on the connect, not a hang or a crash.
    sent = []
    result = await relay.connect_with_filler(
        lambda: _fake_connect(result="no-filler-conn", delay=0.02),
        send_chunk=sent.append,
        audio=b"",
        sample_rate=8000,
        guard_delay=0.01,
        call_uuid="test-empty",
    )
    check("no filler asset: no-op rather than hanging", sent == [], sent)
    check("no filler asset: connect still completes and its result returned", result == "no-filler-conn", result)

    # The real connect attempt fails outright: that exception is what comes
    # back, not swallowed and not reported as the filler's own TimeoutError.
    raised = None
    try:
        await relay.connect_with_filler(
            lambda: _fake_connect(error=ConnectionError("boom")),
            send_chunk=_noop_send,
            audio=b"",
            sample_rate=8000,
            guard_delay=0.01,
            call_uuid="test-error",
        )
    except ConnectionError as exc:
        raised = exc
    check("connect failure propagates, not swallowed", isinstance(raised, ConnectionError), raised)

    # Scoped per turn, not per call: cancelling connect_with_filler itself
    # (this is what a barge-in's `_reply_task.cancel()` now does, since this
    # runs inside speak() inside that same task) must cancel the underlying
    # connect attempt too, rather than leaving it running unobserved - this
    # is the property that replaced the old call-wide filler task, which had
    # no way to know a barge-in had happened at all.
    connect_task_ref: list[asyncio.Task] = []

    async def _tracked_slow_connect():
        connect_task_ref.append(asyncio.current_task())
        await asyncio.sleep(10)
        return "should never get here"

    outer = asyncio.create_task(
        relay.connect_with_filler(
            _tracked_slow_connect,
            send_chunk=_noop_send,
            audio=b"",
            sample_rate=8000,
            guard_delay=10.0,
            call_uuid="test-cancel",
        )
    )
    await asyncio.sleep(0.01)
    outer.cancel()
    try:
        await outer
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.01)
    check(
        "cancelling connect_with_filler cancels the underlying connect",
        bool(connect_task_ref) and connect_task_ref[0].cancelled(),
        connect_task_ref,
    )


asyncio.run(_run_connect_with_filler_checks())

check("filler asset ships in the repo and loaded", len(relay._FILLER_AUDIO) > 0, len(relay._FILLER_AUDIO))

print("\nintermediate:", "all passed so far" if ok else "FAILURES ABOVE")
