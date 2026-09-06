"""
Outbound integrations settings: Google Calendar connect, and this
business's own webhook endpoints. Business-scoped, CurrentUserDep
required throughout except the OAuth callback, which mirrors channels.py's
own Instagram callback exactly - no Krova session reaches that endpoint,
only Google's redirect carrying whatever was round-tripped in `state`.
"""

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.auth import tokens
from shared.auth.encryption import encrypt
from shared.auth.tokens import TokenError
from shared.config.settings import settings
from shared.db.models import CalendarConnection, ConnectionStatus, OutboundWebhook, WebhookEventType
from shared.integrations import google_calendar
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/integrations", tags=["integrations"])


# ── Google Calendar ─────────────────────────────────────────────────────

class ConnectUrlOut(BaseModel):
    url: str


class CalendarStatusOut(BaseModel):
    connected: bool
    status: str | None = None
    connected_at: datetime | None = None


@router.get("/google-calendar/connect-url", response_model=ConnectUrlOut)
async def google_calendar_connect_url(current_user: CurrentUserDep) -> ConnectUrlOut:
    if not settings.google_calendar_client_id or not settings.google_calendar_redirect_uri:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Google Calendar connections are not configured on this server",
        )
    state = tokens.create_connect_state(current_user.business)
    return ConnectUrlOut(url=google_calendar.build_auth_url(state=state))


@router.get("/google-calendar/callback")
async def google_calendar_callback(
    db: DbDep,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    settings_url = f"{settings.frontend_base_url}/settings"

    if error or not code or not state:
        return RedirectResponse(f"{settings_url}?calendar=error")

    try:
        business_id = tokens.decode_connect_state(state)
    except TokenError:
        return RedirectResponse(f"{settings_url}?calendar=expired")

    result = await google_calendar.exchange_code(code)
    if result is None or not result.get("access_token"):
        logger.error("google calendar code exchange failed business=%s", business_id)
        return RedirectResponse(f"{settings_url}?calendar=error")

    existing = await db.execute(
        select(CalendarConnection).where(
            CalendarConnection.business_id == business_id,
            CalendarConnection.provider == "google",
        )
    )
    connection = existing.scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if connection is None:
        connection = CalendarConnection(business_id=business_id, provider="google", connected_at=now)
        db.add(connection)

    connection.access_token = encrypt(result["access_token"])
    if result.get("refresh_token"):
        # Google only returns a refresh_token on the FIRST consent (or when
        # prompt=consent forces a re-issue, which build_auth_url always
        # sets) - never overwrite a good one with a missing value on a
        # later, non-consent token refresh that reuses this same code path.
        connection.refresh_token = encrypt(result["refresh_token"])
    connection.token_expires_at = now + timedelta(seconds=int(result.get("expires_in", 3600)))
    connection.status = ConnectionStatus.active
    connection.connected_at = connection.connected_at or now

    logger.info("google calendar connected business=%s", business_id)
    return RedirectResponse(f"{settings_url}?calendar=connected")


@router.get("/google-calendar", response_model=CalendarStatusOut)
async def google_calendar_status(current_user: CurrentUserDep, db: DbDep) -> CalendarStatusOut:
    result = await db.execute(
        select(CalendarConnection).where(
            CalendarConnection.business_id == current_user.business,
            CalendarConnection.provider == "google",
        )
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        return CalendarStatusOut(connected=False)
    return CalendarStatusOut(
        connected=connection.status == ConnectionStatus.active,
        status=connection.status.value,
        connected_at=connection.connected_at,
    )


@router.post("/google-calendar/disconnect", status_code=status.HTTP_204_NO_CONTENT)
async def google_calendar_disconnect(current_user: CurrentUserDep, db: DbDep) -> None:
    result = await db.execute(
        select(CalendarConnection).where(
            CalendarConnection.business_id == current_user.business,
            CalendarConnection.provider == "google",
        )
    )
    connection = result.scalar_one_or_none()
    if connection is not None:
        connection.status = ConnectionStatus.disconnected
        connection.access_token = None
        connection.refresh_token = None


# ── Outbound webhooks ────────────────────────────────────────────────────

_VALID_EVENTS = {e.value for e in WebhookEventType}


class WebhookIn(BaseModel):
    target_url: str = Field(min_length=1, max_length=2000)
    event_types: list[str] = Field(min_length=1)


class WebhookOut(BaseModel):
    id: str
    target_url: str
    event_types: list[str]
    active: bool
    secret: str | None = None  # only ever returned once, on create
    last_delivery_at: datetime | None
    last_delivery_status: str | None
    failure_count: int


def _out(w: OutboundWebhook, *, reveal_secret: bool = False) -> WebhookOut:
    return WebhookOut(
        id=str(w.id), target_url=w.target_url, event_types=list(w.event_types),
        active=w.active, secret=w.secret if reveal_secret else None,
        last_delivery_at=w.last_delivery_at, last_delivery_status=w.last_delivery_status,
        failure_count=w.failure_count,
    )


def _validate_events(event_types: list[str]) -> None:
    unknown = set(event_types) - _VALID_EVENTS
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown event type(s): {sorted(unknown)}")


@router.get("/webhooks", response_model=list[WebhookOut])
async def list_webhooks(current_user: CurrentUserDep, db: DbDep) -> list[WebhookOut]:
    result = await db.execute(
        select(OutboundWebhook).where(OutboundWebhook.business_id == current_user.business)
    )
    return [_out(w) for w in result.scalars().all()]


@router.post("/webhooks", response_model=WebhookOut, status_code=status.HTTP_201_CREATED)
async def create_webhook(body: WebhookIn, current_user: CurrentUserDep, db: DbDep) -> WebhookOut:
    _validate_events(body.event_types)
    webhook = OutboundWebhook(
        business_id=current_user.business,
        target_url=body.target_url,
        event_types=body.event_types,
        secret=secrets.token_urlsafe(32),
        active=True,
    )
    db.add(webhook)
    await db.flush()
    logger.info("webhook created id=%s business=%s events=%s", webhook.id, current_user.business, body.event_types)
    return _out(webhook, reveal_secret=True)


class WebhookPatch(BaseModel):
    target_url: str | None = None
    event_types: list[str] | None = None
    active: bool | None = None


@router.patch("/webhooks/{webhook_id}", response_model=WebhookOut)
async def update_webhook(webhook_id: uuid.UUID, body: WebhookPatch, current_user: CurrentUserDep, db: DbDep) -> WebhookOut:
    webhook = await db.get(OutboundWebhook, webhook_id)
    if webhook is None or webhook.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")

    if body.event_types is not None:
        _validate_events(body.event_types)
        webhook.event_types = body.event_types
    if body.target_url is not None:
        webhook.target_url = body.target_url
    if body.active is not None:
        webhook.active = body.active

    await db.flush()
    return _out(webhook)


@router.delete("/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(webhook_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
    webhook = await db.get(OutboundWebhook, webhook_id)
    if webhook is None or webhook.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")
    await db.delete(webhook)
