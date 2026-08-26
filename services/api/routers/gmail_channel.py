"""
Connecting a Gmail mailbox.

Google's OAuth differs from Meta's in one way that shapes this file: the
callback arrives as a browser redirect, not an API call, so it carries no
Authorization header. The business it belongs to has to travel in the `state`
parameter and come back intact.

So `state` is a short-lived signed token rather than a random string. An
attacker cannot forge one, it expires in minutes, and it doubles as the CSRF
protection Google's own docs ask for. A plain random value stored server-side
would work too, but it needs a table and a cleanup job to do what a signature
does for free.
"""

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.auth.encryption import decrypt, encrypt
from shared.channels.email import backfill, gmail
from shared.config.settings import settings
from shared.db.models import Channel, ChannelConnection, ConnectionStatus
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/channels/gmail", tags=["channels"])

# Long enough for someone to read Google's consent screen and think about it;
# short enough that a leaked link is worthless.
STATE_TTL_MINUTES = 15


class ConnectUrl(BaseModel):
    authorize_url: str


class BackfillSummary(BaseModel):
    mailbox: str
    messages_read: int
    messages_stored: int
    customers_found: int
    oldest_message: str | None
    newest_message: str | None


def _redirect_uri() -> str:
    if not settings.public_base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PUBLIC_BASE_URL is not configured on this server",
        )
    return f"{settings.public_base_url.rstrip('/')}/api/v1/channels/gmail/callback"


def _make_state(business_id, user_id) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "biz": str(business_id),
            "usr": str(user_id),
            "typ": "gmail_oauth",
            "iat": now,
            "exp": now + timedelta(minutes=STATE_TTL_MINUTES),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def _read_state(state: str) -> dict:
    try:
        claims = jwt.decode(
            state, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This connection link has expired. Please try again.",
        ) from exc
    if claims.get("typ") != "gmail_oauth":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid connection link"
        )
    return claims


@router.get("/connect", response_model=ConnectUrl)
async def connect_gmail(current_user: CurrentUserDep) -> ConnectUrl:
    """Where to send the owner to grant read access to their mailbox."""
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gmail is not configured on this server",
        )
    return ConnectUrl(
        authorize_url=gmail.authorize_url(
            state=_make_state(current_user.business, current_user.id),
            redirect_uri=_redirect_uri(),
        )
    )


@router.get("/callback", response_class=HTMLResponse, include_in_schema=False)
async def gmail_callback(
    db: DbDep,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    """
    Where Google sends the owner back.

    A browser redirect, so it answers with a page rather than JSON. The
    backfill is deliberately not run here - it can take minutes, and a browser
    waiting on it would time out. It is queued instead.
    """
    if error:
        # The commonest is access_denied, which is someone changing their mind.
        # That is not an error worth alarming them about.
        return _page("Connection cancelled", "No changes were made to your account.")

    if not code or not state:
        return _page("Something went wrong", "Google did not return an authorisation.")

    claims = _read_state(state)
    business_id = claims["biz"]

    try:
        tokens = await gmail.exchange_code(code, _redirect_uri())
    except gmail.GmailError as exc:
        logger.error("gmail callback failed for business=%s: %s", business_id, exc)
        return _page("Could not connect", str(exc))

    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token")

    try:
        profile = await gmail.GmailClient(access_token).profile()
    except gmail.GmailError as exc:
        return _page("Could not read the mailbox", str(exc))

    mailbox = (profile.get("emailAddress") or "").lower()
    if not mailbox:
        return _page("Could not connect", "Google did not identify the mailbox.")

    existing = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == business_id,
            ChannelConnection.channel == Channel.email,
            ChannelConnection.external_account_id == mailbox,
        )
    )
    connection = existing.scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if connection is None:
        connection = ChannelConnection(
            business_id=business_id,
            channel=Channel.email,
            external_account_id=mailbox,
            connected_at=now,
        )
        db.add(connection)

    connection.display_name = mailbox
    connection.external_handle = mailbox
    connection.access_token = encrypt(access_token)
    if refresh_token:
        # Google issues one only on first consent. Never overwrite a stored
        # refresh token with nothing, or reconnecting would leave the mailbox
        # readable for an hour and then blind.
        connection.refresh_token = encrypt(refresh_token)
    connection.token_issued_at = now
    connection.token_expires_at = now + timedelta(
        seconds=int(tokens.get("expires_in", 3600))
    )
    connection.status = ConnectionStatus.active
    connection.extra = {**(connection.extra or {}), "scopes": tokens.get("scope", "")}

    await db.flush()

    from shared.db import queue

    await queue.enqueue(
        "gmail_backfill", {"connection_id": str(connection.id)}, db
    )

    logger.info("gmail connected business=%s mailbox=%s", business_id, mailbox)
    return _page(
        "Mailbox connected",
        f"Krova is reading the last {backfill.BACKFILL_DAYS} days of "
        f"{mailbox}. Your commitments will appear shortly.",
    )


@router.post("/backfill", response_model=BackfillSummary)
async def run_backfill_now(current_user: CurrentUserDep, db: DbDep) -> BackfillSummary:
    """
    Read history immediately rather than waiting for the queued job.

    Useful in a demo, where waiting is the wrong kind of drama.
    """
    result = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == current_user.business,
            ChannelConnection.channel == Channel.email,
            ChannelConnection.status == ConnectionStatus.active,
        )
    )
    connection = result.scalars().first()
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No mailbox is connected"
        )

    token = await _usable_token(connection, db)
    outcome = await backfill.run_backfill(connection, token, db)
    await backfill.queue_analysis_for_backfill(current_user.business, db)

    return BackfillSummary(
        mailbox=connection.external_account_id,
        messages_read=outcome.fetched,
        messages_stored=outcome.stored,
        customers_found=outcome.customers_created,
        oldest_message=outcome.oldest.isoformat() if outcome.oldest else None,
        newest_message=outcome.newest.isoformat() if outcome.newest else None,
    )


async def _usable_token(connection: ChannelConnection, db: DbDep) -> str:
    """
    A live access token, refreshing if the stored one has expired.

    Google's last an hour, so this refreshes constantly - unlike Meta's, which
    need a scheduled job because they last sixty days and fail silently.
    """
    now = datetime.now(timezone.utc)
    if connection.token_expires_at and connection.token_expires_at > now + timedelta(
        minutes=2
    ):
        return decrypt(connection.access_token)

    if not connection.refresh_token:
        connection.status = ConnectionStatus.needs_reauth
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Gmail access has expired. Please reconnect the mailbox.",
        )

    try:
        tokens = await gmail.refresh_access_token(decrypt(connection.refresh_token))
    except gmail.GmailError as exc:
        connection.status = ConnectionStatus.needs_reauth
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    access_token = tokens["access_token"]
    connection.access_token = encrypt(access_token)
    connection.token_issued_at = now
    connection.token_expires_at = now + timedelta(
        seconds=int(tokens.get("expires_in", 3600))
    )
    return access_token


def _page(title: str, body: str) -> HTMLResponse:
    """A plain confirmation page. The owner lands here from Google, in a browser."""
    return HTMLResponse(
        f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} &mdash; Krova</title>
<style>
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         background:#0B0B0F; color:#F4F4F5;
         font:16px/1.6 -apple-system,"Segoe UI",system-ui,sans-serif; }}
  main {{ max-width:30rem; padding:2.5rem; text-align:center; }}
  h1 {{ font-size:1.5rem; margin:0 0 .75rem; font-weight:600; }}
  p {{ color:#9A9AA5; margin:0; }}
  .mark {{ width:2rem; height:2rem; border-radius:.45rem; margin:0 auto 1.5rem;
          background:linear-gradient(135deg,#5EEAD4,#00A387); display:grid;
          place-items:center; color:#0B0B0F; font-weight:700; }}
</style>
<main>
  <div class="mark">K</div>
  <h1>{title}</h1>
  <p>{body}</p>
</main>"""
    )
