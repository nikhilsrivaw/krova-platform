"""
The XML Plivo expects back from the Answer URL.

One element: <Stream>, bidirectional, pointed at our WebSocket. Everything
else - greeting, conversation, hangup - happens over that socket once it is
open. There is no <Speak> here and no IVR tree; the whole call is one
continuous stream from the first frame.

mu-law at 8kHz is the wire format every leg of this pipeline already speaks -
Plivo native, Sarvam STT accepts it directly, Sarvam TTS can emit it directly
- so it is the only content type that avoids a transcoding step, and
transcoding is exactly the kind of thing that turns 300ms latency into 900ms.
"""

from xml.sax.saxutils import escape

CONTENT_TYPE = "audio/x-mulaw;rate=8000"


def stream_response(
    websocket_url: str,
    *,
    status_callback_url: str | None = None,
    stream_timeout: int = 3600,
) -> str:
    """
    Build the <Stream> XML that starts a bidirectional call.

    keepCallAlive is required for an AI agent: without it, Plivo tears down
    the call the moment the <Stream> element finishes evaluating, which for a
    bidirectional stream is immediately.
    """
    attrs = [
        'bidirectional="true"',
        'keepCallAlive="true"',
        f'contentType="{CONTENT_TYPE}"',
        f'streamTimeout="{stream_timeout}"',
    ]
    if status_callback_url:
        attrs.append(f'statusCallbackUrl="{escape(status_callback_url)}"')

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"  <Stream {' '.join(attrs)}>{escape(websocket_url)}</Stream>\n"
        "</Response>"
    )


def dial_response(number: str) -> str:
    """
    Bridge the call to a real phone number - a live warm transfer.

    Fetched by Plivo mid-call, via the Transfer API's aleg_url - never
    returned from /voice/answer directly, since the call is already
    answered and streaming by the time a transfer is ever triggered.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f'  <Dial timeout="25"><Number>{escape(number)}</Number></Dial>\n'
        # Reached only when nobody picked up: without this the XML simply ends
        # and the caller is dropped with no word.
        "  <Speak>Sorry, nobody is free to take your call right now. "
        "Someone from the team will call you back soon.</Speak>\n"
        "  <Hangup/>\n"
        "</Response>"
    )


def speak_response(text: str) -> str:
    """
    Read fixed text aloud, then hang up - no <Stream>, no conversation.

    For the one call shape that genuinely needs none of the AI pipeline:
    reading out a login OTP (shared/auth/otp.py). Plivo's own default voice
    (WOMAN, en-US) rather than a Polly voice + SSML - the account's voice
    tier is not confirmed, and plain punctuation-spaced digits ("1. 2. 3.")
    read clearly on any TTS engine without needing SSML support at all.
    Explicit <Hangup/> rather than relying on undocumented behaviour for
    what Plivo does once a Response has no more verbs.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"  <Speak>{escape(text)}</Speak>\n"
        "  <Hangup/>\n"
        "</Response>"
    )


def copilot_response(
    staff_number: str,
    *,
    websocket_url: str | None = None,
    status_callback_url: str | None = None,
    greeting: str | None = None,
) -> str:
    """
    Ring the staff member directly, never the AI's voice.

    `websocket_url`, when given, forks a listen-only copy of the call
    audio to us so the same context-building machinery that drives AI
    replies can show the human real-time suggestions instead of speaking
    them (see copilot.py). `bidirectional` is deliberately false: KROVA
    never sends anything back into this call.

    This is the one part of copilot mode with a real, ongoing AI cost -
    copilot.py's _pump_suggestions runs live STT plus an LLM call per
    final transcript segment for as long as the human conversation lasts,
    unlike every other AI cost in this codebase, which is naturally
    bounded by the AI's own turns. So it is opt-in on its own
    (Business.settings["copilot_live_suggestions"], answer.py decides),
    separate from copilot_mode itself: a business can have the greeting
    and the warm hand-off to staff - the cheap, bounded part - without
    paying for live suggestions it may not even want. Omit
    websocket_url and this is just Speak + Dial, no AI involved once the
    call connects, and copilot_stream/_pump_suggestions never runs at all
    for that call.
    """
    speak_line = f"  <Speak>{escape(greeting)}</Speak>\n" if greeting else ""

    stream_line = ""
    if websocket_url:
        stream_attrs = [
            'bidirectional="false"',
            'audioTrack="both"',
            f'contentType="{CONTENT_TYPE}"',
        ]
        if status_callback_url:
            stream_attrs.append(f'statusCallbackUrl="{escape(status_callback_url)}"')
        stream_line = f"  <Stream {' '.join(stream_attrs)}>{escape(websocket_url)}</Stream>\n"

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"{speak_line}"
        f"{stream_line}"
        f"  <Dial><Number>{escape(staff_number)}</Number></Dial>\n"
        "</Response>"
    )


def getdigits_response(
    prompt_text: str,
    action_url: str,
    *,
    num_digits: int = 1,
    valid_digits: str = "12",
    timeout: int = 8,
    retries: int = 1,
    on_no_input: str = "",
) -> str:
    """
    A deterministic DTMF menu - no AI, no <Stream>, for exactly the kind of
    fixed yes/no decision that should never cost an LLM call (the same
    "deterministic, never agent-mediated" rule already applied to the
    WhatsApp COD button-tap path). Confirmed against Plivo's own
    <GetDigits> docs (plivo.com/docs/voice/xml/getdigits): action receives
    the pressed Digits by POST; on exhausted retries with no input, Plivo
    moves on to the next XML element rather than erroring, which is what
    on_no_input (typically a Hangup, or nothing - see cod_ivr.py) is for.
    """
    attrs = [
        f'action="{escape(action_url)}"',
        'method="POST"',
        f'numDigits="{num_digits}"',
        f'validDigits="{escape(valid_digits)}"',
        f'timeout="{timeout}"',
        f'retries="{retries}"',
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"  <GetDigits {' '.join(attrs)}>\n"
        f"    <Speak>{escape(prompt_text)}</Speak>\n"
        "  </GetDigits>\n"
        f"  {on_no_input}\n"
        "</Response>"
    )


def hangup_response(reason: str | None = None) -> str:
    """
    End the call cleanly.

    Used when we cannot route the call at all - no business owns the number
    that was dialled - rather than opening a stream to nobody.
    """
    comment = f"<!-- {escape(reason)} -->\n  " if reason else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"  {comment}<Hangup/>\n"
        "</Response>"
    )
