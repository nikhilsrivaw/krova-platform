"""
Bridging /voice/answer to /voice/stream.

Plivo's WebSocket `start` event carries only callId/streamId/accountId - no
To/From, confirmed against a real call. The only place those numbers exist is
the /voice/answer webhook, moments earlier. CallUUID from that webhook is the
same value as `start.callId` on the socket, so it is the join key: answer()
remembers the numbers under it, the socket handler recalls and discards them
once the stream actually starts.

Also the only place a genuine "ring started" timestamp can be captured at
all - relay.py's Call row used to set started_at/answered_at to the same
now() at the moment the media stream connects, which is really "the AI
engaged," not "the phone started ringing." The timestamp taken here, the
instant Plivo's answer webhook first arrives, is what
shared/care/voice_trust.py's time-to-answer figure is actually computed
from - see Call.ring_started_at's own docstring.
"""

from datetime import datetime, timezone

_pending: dict[str, dict] = {}


def remember(call_uuid: str, *, to_number: str, from_number: str) -> None:
    _pending[call_uuid] = {
        "to": to_number,
        "from": from_number,
        "ring_started_at": datetime.now(timezone.utc),
    }


def recall(call_uuid: str) -> dict | None:
    return _pending.pop(call_uuid, None)
