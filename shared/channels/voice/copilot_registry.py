"""
In-process fan-out from a live call's copilot suggestions to whichever
staff dashboard has it open.

Same shape and same accepted limitation as call_registry.py: an in-memory
dict, not shared across worker processes. Fine here for the same reason -
the voice service runs as one process, and a live call's copilot feed only
ever needs to reach whoever in that one business has the live-assist page
open right now, not survive a restart.
"""

import asyncio
import uuid

_listeners: dict[uuid.UUID, set[asyncio.Queue]] = {}


def subscribe(business_id: uuid.UUID) -> asyncio.Queue:
    """A browser opened the live-assist page - register a queue for it."""
    queue: asyncio.Queue = asyncio.Queue()
    _listeners.setdefault(business_id, set()).add(queue)
    return queue


def unsubscribe(business_id: uuid.UUID, queue: asyncio.Queue) -> None:
    listeners = _listeners.get(business_id)
    if listeners is not None:
        listeners.discard(queue)
        if not listeners:
            _listeners.pop(business_id, None)


def publish(business_id: uuid.UUID, message: dict) -> None:
    """A suggestion is ready - hand it to every dashboard watching this business."""
    for queue in _listeners.get(business_id, ()):
        queue.put_nowait(message)
