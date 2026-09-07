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
        f"  <Dial><Number>{escape(number)}</Number></Dial>\n"
        "</Response>"
    )


def copilot_response(
    websocket_url: str,
    staff_number: str,
    *,
    status_callback_url: str | None = None,
) -> str:
    """
    Live copilot mode: ring the staff member directly, never the AI's
    voice - while forking a listen-only copy of the audio to us so the
    same context-building machinery that drives AI replies can show the
    human real-time suggestions instead of speaking them. `bidirectional`
    is deliberately false: KROVA never sends anything back into this call.
    """
    stream_attrs = [
        'bidirectional="false"',
        'audioTrack="both"',
        f'contentType="{CONTENT_TYPE}"',
    ]
    if status_callback_url:
        stream_attrs.append(f'statusCallbackUrl="{escape(status_callback_url)}"')

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"  <Stream {' '.join(stream_attrs)}>{escape(websocket_url)}</Stream>\n"
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
