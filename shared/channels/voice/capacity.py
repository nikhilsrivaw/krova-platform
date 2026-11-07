"""
How many phone lines KROVA has in use, and whether a campaign may dial another.

Plivo gives the whole account (every business together) a fixed number of calls at once - 50, confirmed by Plivo
support on 2026-11-07 - and 2 new outbound calls a second. A call past either limit fails at once; nothing waits in
line. Campaigns used to dial back to back with no check, so one large campaign could fill every line and a caller
ringing an admission helpline would get nothing.

So a campaign dials at most one call a second, and stops dialling while fewer than VOICE_INBOUND_RESERVE lines are
free, leaving them for people calling in. One-off calls (an OTP, a team member's call) are left alone: they are few,
and each matters to someone waiting.

Lines in use are read from Plivo itself, not only from our call rows. A call put through to a team member is a
bridge, and Plivo counts both of its legs (support, 2026-11-08); our rows miss it entirely - a call that rings the
team straight away never gets a row, and an agent call's row closes the moment it is transferred. Each business's
calls live on its own Plivo subaccount, so every subaccount is asked. If Plivo cannot be reached, our own count is
used instead. The leg to the team member is its own Plivo call on the same subaccount (its own uuid and CDR - see
human_calls.record_leg_costs), so the live list should show a bridge as two; not yet watched on a live call.
"""

import asyncio
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import httpx
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


PLIVO_BASE = "https://api.plivo.com/v1/Account"
PLIVO_TIMEOUT = 5.0
# Asking Plivo means one request per subaccount, so a reading is reused for this long. Dials made in between are
# still counted by the Dialler as ringing calls.
LIVE_READING_SECONDS = 10.0
# Businesses are added rarely; the subaccount list is fetched again after this long.
SUBACCOUNTS_SECONDS = 600.0

_live: tuple[float, int] | None = None
_subaccounts: tuple[float, list[tuple[str, str]]] | None = None


async def _subaccount_logins(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    global _subaccounts
    now = time.monotonic()
    if _subaccounts and now - _subaccounts[0] < SUBACCOUNTS_SECONDS:
        return _subaccounts[1]
    main = httpx.BasicAuth(settings.plivo_auth_id, settings.plivo_auth_token)
    logins: list[tuple[str, str]] = []
    offset = 0
    while True:
        res = await client.get(
            f"{PLIVO_BASE}/{settings.plivo_auth_id}/Subaccount/", params={"limit": 20, "offset": offset}, auth=main
        )
        res.raise_for_status()
        page = res.json().get("objects") or []
        logins += [(s["auth_id"], s["auth_token"]) for s in page if s.get("auth_id") and s.get("auth_token")]
        if len(page) < 20:
            break
        offset += 20
    _subaccounts = (now, logins)
    return logins


async def _live_legs(client: httpx.AsyncClient, auth_id: str, auth_token: str) -> int:
    res = await client.get(
        f"{PLIVO_BASE}/{auth_id}/Call/", params={"status": "live"}, auth=httpx.BasicAuth(auth_id, auth_token)
    )
    res.raise_for_status()
    return len(res.json().get("calls") or [])


async def plivo_lines_in_use() -> int | None:
    """Live call legs on the main account and every subaccount, as Plivo sees them. None if Plivo could not say."""
    global _live
    now = time.monotonic()
    if _live and now - _live[0] < LIVE_READING_SECONDS:
        return _live[1]
    if not settings.plivo_auth_id or not settings.plivo_auth_token:
        return None
    try:
        async with httpx.AsyncClient(timeout=PLIVO_TIMEOUT) as client:
            logins = [(settings.plivo_auth_id, settings.plivo_auth_token), *await _subaccount_logins(client)]
            counts = await asyncio.gather(*(_live_legs(client, a, t) for a, t in logins))
    except Exception as exc:  # noqa: BLE001 - our own count is the fallback
        logger.warning("could not read live calls from plivo, using our own count: %s", type(exc).__name__)
        return None
    _live = (now, sum(counts))
    return _live[1]


async def lines_in_use(db: AsyncSession) -> int:
    """
    The larger of Plivo's live legs and our own open calls. Plivo sees team calls we cannot; our rows can be ahead of
    Plivo for a moment just after a call starts. Taking the larger errs on the side of leaving lines free.
    """
    own = await open_calls(db)
    plivo = await plivo_lines_in_use()
    return own if plivo is None else max(own, plivo)


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
