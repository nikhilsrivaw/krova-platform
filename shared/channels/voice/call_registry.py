"""
Bridging /voice/answer to /voice/stream.

Plivo's WebSocket `start` event carries only callId/streamId/accountId - no
To/From, confirmed against a real call. The only place those numbers exist is
the /voice/answer webhook, moments earlier. CallUUID from that webhook is the
same value as `start.callId` on the socket, so it is the join key: answer()
remembers the numbers under it, the socket handler recalls and discards them
once the stream actually starts.
"""

_pending: dict[str, dict[str, str]] = {}


def remember(call_uuid: str, *, to_number: str, from_number: str) -> None:
    _pending[call_uuid] = {"to": to_number, "from": from_number}


def recall(call_uuid: str) -> dict[str, str] | None:
    return _pending.pop(call_uuid, None)
