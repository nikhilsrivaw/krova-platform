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

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.auth import tokens
from shared.auth.encryption import decrypt, encrypt
from shared.auth.tokens import TokenError
from shared.config.settings import settings
from shared.db.models import (
    ApiKey,
    Business,
    CalendarConnection,
    ConnectionStatus,
    EmailSendConnection,
    GitHubConnection,
    OutboundWebhook,
    StripeConnection,
    WebhookEventType,
)
from shared.integrations import api_keys as api_keys_module
from shared.integrations import github as github_module
from shared.integrations import google_calendar
from shared.integrations import postmark
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


# ── GitHub (software-startup vertical's closed bug-lifecycle loop) ──────
#
# v1 is a pasted fine-grained PAT + a webhook secret the business chose
# themselves in their own repo's Settings > Webhooks screen - the same
# "paste credentials generated in the platform's own dashboard" shape as
# ShippingConnection above, and for the identical reason: no self-serve
# GitHub App/OAuth flow is built here, that is a bigger, separate effort.

class GitHubConnectionIn(BaseModel):
    repo_owner: str = Field(min_length=1, max_length=255)
    repo_name: str = Field(min_length=1, max_length=255)
    access_token: str = Field(min_length=1, max_length=500)
    webhook_secret: str = Field(min_length=1, max_length=500)


class GitHubConnectionOut(BaseModel):
    id: str
    repo_owner: str
    repo_name: str
    status: str
    connected_at: datetime | None
    # access_token/webhook_secret deliberately absent - never returned
    # once stored, same rule as every other credential in this router.


def _github_out(c: GitHubConnection) -> GitHubConnectionOut:
    return GitHubConnectionOut(
        id=str(c.id), repo_owner=c.repo_owner, repo_name=c.repo_name,
        status=c.status.value, connected_at=c.connected_at,
    )


@router.get("/github", response_model=GitHubConnectionOut | None)
async def github_status(current_user: CurrentUserDep, db: DbDep) -> GitHubConnectionOut | None:
    result = await db.execute(
        select(GitHubConnection).where(GitHubConnection.business_id == current_user.business)
    )
    connection = result.scalar_one_or_none()
    return _github_out(connection) if connection else None


@router.post("/github", response_model=GitHubConnectionOut, status_code=status.HTTP_201_CREATED)
async def connect_github(body: GitHubConnectionIn, current_user: CurrentUserDep, db: DbDep) -> GitHubConnectionOut:
    # A real check, not just stored credentials - confirms the token can
    # actually see this repo before the connection is saved, same
    # "prove it before storing it" instinct as Shiprocket's own real
    # login check.
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(
            f"{github_module.API_BASE}/repos/{body.repo_owner}/{body.repo_name}",
            headers={"Authorization": f"Bearer {body.access_token}", "Accept": "application/vnd.github+json"},
        )
    if res.status_code != 200:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Could not access {body.repo_owner}/{body.repo_name} with this token ({res.status_code})",
        )

    existing = (
        await db.execute(select(GitHubConnection).where(GitHubConnection.business_id == current_user.business))
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if existing is None:
        existing = GitHubConnection(business_id=current_user.business)
        db.add(existing)

    existing.repo_owner = body.repo_owner
    existing.repo_name = body.repo_name
    existing.access_token = encrypt(body.access_token)
    existing.webhook_secret = encrypt(body.webhook_secret)
    existing.status = ConnectionStatus.active
    existing.connected_at = existing.connected_at or now

    await db.flush()
    logger.info("github connected business=%s repo=%s/%s", current_user.business, body.repo_owner, body.repo_name)
    return _github_out(existing)


@router.delete("/github", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_github(current_user: CurrentUserDep, db: DbDep) -> None:
    result = await db.execute(
        select(GitHubConnection).where(GitHubConnection.business_id == current_user.business)
    )
    connection = result.scalar_one_or_none()
    if connection is not None:
        connection.status = ConnectionStatus.disconnected


# ── Outbound email (software-startup vertical - the "it's fixed" send) ──
#
# See shared/integrations/postmark.py's own docstring for why this is
# one shared Krova-level Postmark account with a per-business sender
# signature, not a per-business OAuth mailbox connection.

class EmailConnectionIn(BaseModel):
    from_email: str = Field(min_length=3, max_length=320)


class EmailConnectionOut(BaseModel):
    id: str
    from_email: str
    verified: bool
    connected_at: datetime | None


def _email_out(c: EmailSendConnection) -> EmailConnectionOut:
    return EmailConnectionOut(
        id=str(c.id), from_email=c.from_email, verified=c.verified, connected_at=c.connected_at,
    )


@router.get("/email-connection", response_model=EmailConnectionOut | None)
async def email_connection_status(current_user: CurrentUserDep, db: DbDep) -> EmailConnectionOut | None:
    """
    Also re-polls Postmark for a not-yet-verified signature, so the
    settings screen reflects a confirmation click without a separate
    "check status" button - cheap, since this is a low-traffic page.
    """
    result = await db.execute(
        select(EmailSendConnection).where(EmailSendConnection.business_id == current_user.business)
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        return None
    if not connection.verified:
        try:
            connection.verified = await postmark.check_signature_verified(connection.postmark_signature_id)
        except postmark.PostmarkError:
            pass  # left as-is; the next poll tries again
    return _email_out(connection)


@router.post("/email-connection", response_model=EmailConnectionOut, status_code=status.HTTP_201_CREATED)
async def connect_email(body: EmailConnectionIn, current_user: CurrentUserDep, db: DbDep) -> EmailConnectionOut:
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")

    try:
        signature = await postmark.create_signature(body.from_email, business_name=business.name)
    except postmark.PostmarkError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    existing = (
        await db.execute(
            select(EmailSendConnection).where(EmailSendConnection.business_id == current_user.business)
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if existing is None:
        existing = EmailSendConnection(business_id=current_user.business)
        db.add(existing)

    existing.from_email = body.from_email
    existing.postmark_signature_id = signature.id
    existing.verified = signature.confirmed
    existing.connected_at = existing.connected_at or now

    await db.flush()
    logger.info("email connection created business=%s from=%s", current_user.business, body.from_email)
    return _email_out(existing)


@router.delete("/email-connection", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_email(current_user: CurrentUserDep, db: DbDep) -> None:
    result = await db.execute(
        select(EmailSendConnection).where(EmailSendConnection.business_id == current_user.business)
    )
    connection = result.scalar_one_or_none()
    if connection is not None:
        await db.delete(connection)


# ── Stripe (software-startup vertical - billing dunning) ────────────────
#
# See StripeConnection's own docstring for why the webhook URL itself
# (not a header) is the per-business lookup key. Krova only ever
# generates the URL's token; the business generates the signing secret
# themselves in their own Stripe Dashboard and pastes it here.

class StripeConnectionIn(BaseModel):
    # Optional on purpose: the real-world order is generate-the-URL-first,
    # paste-it-into-Stripe, THEN Stripe hands back a secret to paste here -
    # a business cannot have the secret before Krova has issued the URL.
    # Calling this with no secret just (re)issues the URL; calling it
    # again with one fills it in, same connection, same token/URL.
    webhook_secret: str | None = Field(default=None, max_length=500)


class StripeConnectionOut(BaseModel):
    id: str
    webhook_url: str
    status: str
    connected_at: datetime | None
    # Whether a real signing secret has been saved yet - lets the
    # settings screen show "step 2 still needed" without ever returning
    # the secret itself, which stays absent here on purpose.
    has_secret: bool = False


def _stripe_webhook_url(webhook_token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/webhooks/stripe/{webhook_token}"


@router.get("/stripe", response_model=StripeConnectionOut | None)
async def stripe_status(current_user: CurrentUserDep, db: DbDep) -> StripeConnectionOut | None:
    result = await db.execute(
        select(StripeConnection).where(StripeConnection.business_id == current_user.business)
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        return None
    return StripeConnectionOut(
        id=str(connection.id), webhook_url=_stripe_webhook_url(connection.webhook_token),
        status=connection.status.value, connected_at=connection.connected_at,
        has_secret=bool(connection.webhook_secret and decrypt(connection.webhook_secret)),
    )


@router.post("/stripe", response_model=StripeConnectionOut, status_code=status.HTTP_201_CREATED)
async def connect_stripe(body: StripeConnectionIn, current_user: CurrentUserDep, db: DbDep) -> StripeConnectionOut:
    existing = (
        await db.execute(select(StripeConnection).where(StripeConnection.business_id == current_user.business))
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if existing is None:
        # No secret yet is the expected first call - a business cannot
        # have Stripe's own secret before Krova has issued this URL for
        # them to paste into their Stripe Dashboard. Placeholder secret
        # (empty string, encrypted) means every webhook 403s until they
        # come back with the real one - self-correcting, no separate
        # "pending" status needed.
        connection = StripeConnection(
            business_id=current_user.business,
            webhook_token=secrets.token_urlsafe(32),
            webhook_secret=encrypt(body.webhook_secret or ""),
            status=ConnectionStatus.active,
            connected_at=now,
        )
        db.add(connection)
    else:
        connection = existing
        # Never blanks an already-saved secret - a call to just re-fetch
        # the URL (no secret in the body) must not undo a working setup.
        if body.webhook_secret:
            connection.webhook_secret = encrypt(body.webhook_secret)
        connection.status = ConnectionStatus.active
        connection.connected_at = connection.connected_at or now

    await db.flush()
    logger.info("stripe connected business=%s", current_user.business)
    return StripeConnectionOut(
        id=str(connection.id), webhook_url=_stripe_webhook_url(connection.webhook_token),
        status=connection.status.value, connected_at=connection.connected_at,
        has_secret=bool(connection.webhook_secret and decrypt(connection.webhook_secret)),
    )


@router.delete("/stripe", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_stripe(current_user: CurrentUserDep, db: DbDep) -> None:
    result = await db.execute(
        select(StripeConnection).where(StripeConnection.business_id == current_user.business)
    )
    connection = result.scalar_one_or_none()
    if connection is not None:
        connection.status = ConnectionStatus.disconnected


# ── Outbound webhooks ────────────────────────────────────────────────────

_VALID_EVENTS = {e.value for e in WebhookEventType}


_VALID_FORMATS = {"raw", "slack", "teams"}


class WebhookIn(BaseModel):
    target_url: str = Field(min_length=1, max_length=2000)
    event_types: list[str] = Field(min_length=1)
    format: str = "raw"


class WebhookOut(BaseModel):
    id: str
    target_url: str
    event_types: list[str]
    active: bool
    format: str
    secret: str | None = None  # only ever returned once, on create
    last_delivery_at: datetime | None
    last_delivery_status: str | None
    failure_count: int


def _out(w: OutboundWebhook, *, reveal_secret: bool = False) -> WebhookOut:
    return WebhookOut(
        id=str(w.id), target_url=w.target_url, event_types=list(w.event_types),
        active=w.active, format=w.format, secret=w.secret if reveal_secret else None,
        last_delivery_at=w.last_delivery_at, last_delivery_status=w.last_delivery_status,
        failure_count=w.failure_count,
    )


def _validate_events(event_types: list[str]) -> None:
    unknown = set(event_types) - _VALID_EVENTS
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown event type(s): {sorted(unknown)}")


def _validate_format(fmt: str) -> None:
    if fmt not in _VALID_FORMATS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown format {fmt!r} - must be one of {sorted(_VALID_FORMATS)}")


@router.get("/webhooks", response_model=list[WebhookOut])
async def list_webhooks(current_user: CurrentUserDep, db: DbDep) -> list[WebhookOut]:
    result = await db.execute(
        select(OutboundWebhook).where(OutboundWebhook.business_id == current_user.business)
    )
    return [_out(w) for w in result.scalars().all()]


@router.post("/webhooks", response_model=WebhookOut, status_code=status.HTTP_201_CREATED)
async def create_webhook(body: WebhookIn, current_user: CurrentUserDep, db: DbDep) -> WebhookOut:
    _validate_events(body.event_types)
    _validate_format(body.format)
    webhook = OutboundWebhook(
        business_id=current_user.business,
        target_url=body.target_url,
        event_types=body.event_types,
        format=body.format,
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
    format: str | None = None


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
    if body.format is not None:
        _validate_format(body.format)
        webhook.format = body.format

    await db.flush()
    return _out(webhook)


# ── API keys ─────────────────────────────────────────────────────────────

class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class ApiKeyOut(BaseModel):
    id: str
    name: str
    key_prefix: str
    active: bool
    last_used_at: datetime | None
    raw_key: str | None = None  # only ever returned once, on create


def _api_key_out(k: ApiKey, *, reveal: str | None = None) -> ApiKeyOut:
    return ApiKeyOut(
        id=str(k.id), name=k.name, key_prefix=k.key_prefix, active=k.active,
        last_used_at=k.last_used_at, raw_key=reveal,
    )


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(current_user: CurrentUserDep, db: DbDep) -> list[ApiKeyOut]:
    result = await db.execute(select(ApiKey).where(ApiKey.business_id == current_user.business))
    return [_api_key_out(k) for k in result.scalars().all()]


@router.post("/api-keys", response_model=ApiKeyOut, status_code=status.HTTP_201_CREATED)
async def create_api_key(body: ApiKeyIn, current_user: CurrentUserDep, db: DbDep) -> ApiKeyOut:
    raw_key, key_hash, prefix = api_keys_module.generate()
    key = ApiKey(business_id=current_user.business, name=body.name, key_hash=key_hash, key_prefix=prefix, active=True)
    db.add(key)
    await db.flush()
    logger.info("api key created id=%s business=%s", key.id, current_user.business)
    return _api_key_out(key, reveal=raw_key)


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_key(key_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
    key = await db.get(ApiKey, key_id)
    if key is None or key.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    await db.delete(key)


@router.delete("/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(webhook_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
    webhook = await db.get(OutboundWebhook, webhook_id)
    if webhook is None or webhook.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")
    await db.delete(webhook)
