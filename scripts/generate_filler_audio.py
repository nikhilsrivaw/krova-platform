"""
One-time generator for the voice agent's connect-time filler clip.

Not part of the live call path. Sarvam's TTS handshake occasionally takes
several seconds longer than usual (confirmed on real calls, see
`_connect_sarvam`'s retry-at-longer-timeout in relay.py) - long enough that a
caller hears pure silence and hangs up before the greeting ever starts. The
fix is to have a few seconds of pre-baked, already-decided audio ready to
play the instant the call connects, bought with zero network round-trips,
while the real greeting connection happens in the background.

Run once, whenever the filler phrase or voice changes:

    python scripts/generate_filler_audio.py

Writes raw mu-law/8kHz bytes (no container, no header - exactly the wire
format `send_audio` in relay.py already expects) to
shared/channels/voice/assets/filler_en.ulaw.
"""

import asyncio
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.channels.voice import sarvam  # noqa: E402

OUT_PATH = (
    Path(__file__).resolve().parent.parent
    / "shared"
    / "channels"
    / "voice"
    / "assets"
    / "filler_en.ulaw"
)

# Neutral and true regardless of what the business turns out to be - it
# never claims to have understood anything yet. Deliberately longer than the
# first version ("One moment please.", 0.82s) - measured against five real
# calls, the gap between the guard delay and the greeting's actual connect
# time ranged 0.14-1.13s, so an 0.82s clip was long enough to need a second
# full loop on the slower calls, and there is no way to stop a loop mid-clip
# once it has been sent to Plivo. A single ~1.5s play comfortably covers the
# whole observed range without looping, so it reads as one sentence instead
# of an audibly repeated phrase.
FILLER_TEXT = "One moment please, I'm just connecting you now."


async def main() -> None:
    headers = sarvam.auth_headers()
    url = sarvam.tts_connect_url()
    chunks: list[bytes] = []

    async with websockets.connect(url, additional_headers=headers, open_timeout=20) as ws:
        await ws.send(sarvam.tts_start_config(language="en-IN"))
        await ws.send(sarvam.tts_text_frame(FILLER_TEXT))
        await ws.send(sarvam.tts_flush_frame())

        async for raw in ws:
            if sarvam.is_tts_final_event(raw):
                break
            chunk = sarvam.parse_tts_event(raw)
            if chunk is not None:
                chunks.append(chunk)

    audio = b"".join(chunks)
    if not audio:
        raise RuntimeError("Sarvam returned no audio for the filler phrase")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_bytes(audio)

    seconds = len(audio) / 8000  # mu-law is 1 byte/sample at 8kHz
    print(f"wrote {len(audio)} bytes ({seconds:.2f}s) to {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
