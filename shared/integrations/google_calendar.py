"""
One-way sync: a booked Appointment or issued QueueEntry token creates a
matching event on a business's own Google Calendar. Krova never reads
from the calendar - no conflict-resolution direction to build, because
there's only ever one writer.

OAuth token refresh mirrors shared/channels/whatsapp/token_refresh.py's
own shape (a REFRESH_WINDOW headroom, old token stays valid until refresh
succeeds), adapted for Google's token endpoint instead of Meta's.

Every public function here fails closed: logs and returns rather than
raising. A calendar sync failure must never block or roll back the
booking that triggered it - the same "downgrade gracefully" contract
shared/scheduling/queue_booking.py's own notify.send_queue_checkin call
already follows for its own best-effort side channel.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt, encrypt
from shared.config.settings import settings
from shared.db.models import Appointment, Business, CalendarConnection, ConnectionStatus, QueueEntry
from shared.utils.logging import get_logger

logger = get_logger(__name__)

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_EVENTS_BASE = "https://www.googleapis.com/calendar/v3/calendars"
_SCOPE = "https://www.googleapis.com/auth/calendar.events"

REFRESH_WINDOW = timedelta(minutes=10)

# A queue token has no natural duration (unlike an Appointment's own
# starts_at/ends_at) - blocked out as a short placeholder on the calendar,
# just enough to be visible without implying a real consultation length.
QUEUE_EVENT_DURATION = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_auth_url(*, state: str) -> str:
    """Where the 'Connect Google Calendar' button in Settings sends the owner."""
    from urllib.parse import urlencode

    params = {
        "client_id": settings.google_calendar_client_id,
        "redirect_uri": settings.google_calendar_redirect_uri,
        "response_type": "code",
        "scope": _SCOPE,
        "access_type": "offline",
        # Forces Google to issue a refresh_token even on a re-connect -
        # without this, a second consent for the same account silently
        # returns no refresh_token at all, and the connection can't
        # outlive the first access token's hour.
        "prompt": "consent",
        "state": state,
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict | None:
    """Trade an OAuth `code` for tokens. None on any failure - the callback
    route turns that into an honest "connection failed" rather than a 500."""
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.post(
                _TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.google_calendar_client_id,
                    "client_secret": settings.google_calendar_client_secret,
                    "redirect_uri": settings.google_calendar_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
    except httpx.HTTPError as exc:
        logger.error("google calendar code exchange failed: %s", exc)
        return None

    if response.status_code != 200:
        logger.error("google calendar code exchange rejected status=%s: %s", response.status_code, response.text[:300])
        return None
    return response.json()


def _needs_refresh(connection: CalendarConnection) -> bool:
    if connection.status != ConnectionStatus.active or not connection.refresh_token:
        return False
    if connection.token_expires_at is None:
        return True
    expires = connection.token_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires - _now() <= REFRESH_WINDOW


async def _refresh(connection: CalendarConnection) -> bool:
    try:
        refresh_token = decrypt(connection.refresh_token or "")
    except Exception:
        logger.exception("could not decrypt calendar refresh token connection=%s", connection.id)
        return False

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.post(
                _TOKEN_URL,
                data={
                    "refresh_token": refresh_token,
                    "client_id": settings.google_calendar_client_id,
                    "client_secret": settings.google_calendar_client_secret,
                    "grant_type": "refresh_token",
                },
            )
    except httpx.HTTPError as exc:
        logger.error("google calendar token refresh request failed connection=%s: %s", connection.id, exc)
        return False

    if response.status_code != 200:
        logger.error(
            "google calendar token refresh rejected connection=%s status=%s: %s",
            connection.id, response.status_code, response.text[:300],
        )
        return False

    payload = response.json()
    token = payload.get("access_token")
    if not token:
        return False

    connection.access_token = encrypt(token)
    connection.token_expires_at = _now() + timedelta(seconds=int(payload.get("expires_in", 3600)))
    return True


async def _valid_access_token(db: AsyncSession, connection: CalendarConnection) -> str | None:
    if _needs_refresh(connection):
        if not await _refresh(connection):
            connection.status = ConnectionStatus.needs_reauth
            await db.flush()
            return None
        await db.flush()
    try:
        return decrypt(connection.access_token or "")
    except Exception:
        logger.exception("could not decrypt calendar access token connection=%s", connection.id)
        return None


async def refresh_expiring(db: AsyncSession) -> dict[str, int]:
    """
    Renew every Google Calendar token close to expiring - same shape and
    same reasoning as shared/channels/whatsapp/token_refresh.py's own
    refresh_expiring, for the Calendar table instead of ChannelConnection.
    """
    result = await db.execute(
        select(CalendarConnection).where(CalendarConnection.status == ConnectionStatus.active)
    )
    due = [c for c in result.scalars().all() if _needs_refresh(c)]

    refreshed = failed = 0
    for connection in due:
        if await _refresh(connection):
            refreshed += 1
        else:
            failed += 1
            connection.status = ConnectionStatus.needs_reauth

    if due:
        await db.commit()
    if failed:
        logger.error("calendar token refresh: %s renewed, %s FAILED", refreshed, failed)
    elif refreshed:
        logger.info("calendar token refresh: %s renewed", refreshed)

    return {"considered": len(due), "refreshed": refreshed, "failed": failed}


async def get_connection(db: AsyncSession, *, business_id: uuid.UUID) -> CalendarConnection | None:
    result = await db.execute(
        select(CalendarConnection).where(
            CalendarConnection.business_id == business_id,
            CalendarConnection.provider == "google",
            CalendarConnection.status == ConnectionStatus.active,
        )
    )
    return result.scalars().first()


async def _upsert_event(
    db: AsyncSession, *, connection: CalendarConnection, event_id: str | None,
    summary: str, description: str, starts_at: datetime, ends_at: datetime,
) -> str | None:
    token = await _valid_access_token(db, connection)
    if token is None:
        return None

    calendar_id = connection.external_calendar_id or "primary"
    body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": starts_at.isoformat()},
        "end": {"dateTime": ends_at.isoformat()},
    }
    method, url = (
        ("PUT", f"{_EVENTS_BASE}/{calendar_id}/events/{event_id}")
        if event_id else
        ("POST", f"{_EVENTS_BASE}/{calendar_id}/events")
    )

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.request(
                method, url, json=body, headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        logger.error("google calendar event upsert failed connection=%s: %s", connection.id, exc)
        return None

    if response.status_code not in (200, 201):
        logger.error(
            "google calendar event upsert rejected connection=%s status=%s: %s",
            connection.id, response.status_code, response.text[:300],
        )
        return None
    return response.json().get("id")


async def _delete_event(db: AsyncSession, *, connection: CalendarConnection, event_id: str) -> None:
    token = await _valid_access_token(db, connection)
    if token is None:
        return
    calendar_id = connection.external_calendar_id or "primary"
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            await client.delete(
                f"{_EVENTS_BASE}/{calendar_id}/events/{event_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        logger.error("google calendar event delete failed connection=%s: %s", connection.id, exc)


async def sync_appointment(
    db: AsyncSession, *, business: Business, appointment: Appointment, action: Literal["upsert", "cancel"],
) -> None:
    connection = await get_connection(db, business_id=business.id)
    if connection is None:
        return

    if action == "cancel":
        if appointment.google_calendar_event_id:
            await _delete_event(db, connection=connection, event_id=appointment.google_calendar_event_id)
            appointment.google_calendar_event_id = None
        return

    event_id = await _upsert_event(
        db, connection=connection, event_id=appointment.google_calendar_event_id,
        summary=f"Appointment - {business.name}",
        description=f"Booked via Krova (intake_channel={appointment.intake_channel.value})",
        starts_at=appointment.starts_at, ends_at=appointment.ends_at,
    )
    if event_id:
        appointment.google_calendar_event_id = event_id


async def sync_queue_entry(
    db: AsyncSession, *, business: Business, entry: QueueEntry, action: Literal["upsert", "cancel"],
) -> None:
    connection = await get_connection(db, business_id=business.id)
    if connection is None:
        return

    if action == "cancel":
        if entry.google_calendar_event_id:
            await _delete_event(db, connection=connection, event_id=entry.google_calendar_event_id)
            entry.google_calendar_event_id = None
        return

    event_id = await _upsert_event(
        db, connection=connection, event_id=entry.google_calendar_event_id,
        summary=f"Queue token #{entry.queue_number} - {entry.shift.value}",
        description=f"Checked in via Krova (intake_channel={entry.intake_channel.value})",
        starts_at=entry.checked_in_at, ends_at=entry.checked_in_at + QUEUE_EVENT_DURATION,
    )
    if event_id:
        entry.google_calendar_event_id = event_id
