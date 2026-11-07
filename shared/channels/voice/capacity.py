"""
How many phone lines KROVA has in use, and whether a campaign may dial another.

Plivo gives the whole account (every business together) a fixed number of calls at once - 50, confirmed by Plivo
support on 2026-11-07 - and 2 new outbound calls a second. A call past either limit fails at once; nothing waits in
line. Campaigns used to dial back to back with no check, so one large campaign could fill every line and a caller
ringing an admission helpline would get nothing.

So a campaign dials at most one call a second, and stops dialling while fewer than VOICE_INBOUND_RESERVE lines are
free, leaving them for people calling in. One-off calls (an OTP, a team member's call) are left alone: they are few,
and each matters to someone waiting.
"""

import time
from collections import deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.settings import settings
from shared.db.models import Call
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# A call row left open longer than the longest call allowed (20 minutes, pipeline._MAX_CALL_SECONDS) did not close
# cleanly. Counting it forever would slowly block every campaign.
STALE_AFTER = timedelta(minutes=25)
# Plivo allows 2 a second; one leaves room for the one-off calls that go out at the same time.
SECONDS_BETWEEN_DIALS = 1.0
# A dialled call holds a line while it rings, before it has a call row. About this long at most.
RINGING_SECONDS = 60.0
WAIT_WHEN_FULL_SECONDS = 30


def limit() -> int:
    return max(1, settings.voice_concurrent_call_limit)


def campaign_ceiling() -> int:
    """Lines a campaign may use: all of them except the ones kept for incoming calls."""
    return max(0, limit() - max(0, settings.voice_inbound_reserve))


async def open_calls(db: AsyncSession) -> int:
    """Calls in progress right now, across every business."""
    since = datetime.now(timezone.utc) - STALE_AFTER
    count = await db.scalar(select(func.count()).select_from(Call).where(Call.ended_at.is_(None), Call.started_at > since))
    return int(count or 0)


class Dialler:
    """Paces one campaign worker's dials and counts the calls it has left ringing."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._dials: deque[float] = deque()

    def _ringing(self) -> int:
        now = self._clock()
        while self._dials and now - self._dials[0] > RINGING_SECONDS:
            self._dials.popleft()
        return len(self._dials)

    def wait_before_next(self) -> float:
        """Seconds to wait so dials stay at least SECONDS_BETWEEN_DIALS apart."""
        if not self._dials:
            return 0.0
        return max(0.0, SECONDS_BETWEEN_DIALS - (self._clock() - self._dials[-1]))

    def has_room(self, open_now: int) -> bool:
        """
        Whether one more dial stays under the campaign ceiling. A ringing call that was answered is also an open call
        row by then, so this can count it twice: it errs on the side of leaving lines free.
        """
        in_use = open_now + self._ringing()
        if in_use >= campaign_ceiling():
            logger.warning(
                "campaign dialling paused: %s of %s lines in use, %s kept for incoming calls",
                in_use, limit(), settings.voice_inbound_reserve,
            )
            return False
        return True

    def dialled(self) -> None:
        self._dials.append(self._clock())
