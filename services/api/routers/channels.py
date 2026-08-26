"""
Connecting and disconnecting channels.
"""

import secrets
from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.auth.encryption import encrypt
from shared.channels.whatsapp import signup
from shared.config.settings import settings
from shared.db.models import Channel, ChannelConnection, ConnectionStatus
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/channels", tags=["channels"])


class SignupConfig(BaseModel):
    app_id: str
    config_id: str
    graph_version: str


class EmbeddedSignupBody(BaseModel):
    code: str


class GraphCallOut(BaseModel):
    method: str
    path: str
    permission: str
    status: int


class ConnectionOut(BaseModel):
    connected: bool
    channel: str
    waba_id: str
    waba_name: str | None
    phone_number_id: str
    display_phone_number: str | None
    verified_name: str | None
    quality_rating: str | None
    webhook_subscribed: bool
    number_registered: bool
    token_expires_at: str | None
    graph_calls: list[GraphCallOut] = []


@router.get("/whatsapp/signup-config", response_model=SignupConfig)
async def whatsapp_signup_config(current_user: CurrentUserDep) -> SignupConfig:
    """
    What the browser needs to open Meta's signup dialog.

    Served from the API rather than baked into the frontend so the
    configuration can change without a redeploy.
    """
    if not settings.meta_app_id or not settings.meta_config_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp signup is not configured on this server",
        )
    return SignupConfig(
        app_id=settings.meta_app_id,
        config_id=settings.meta_config_id,
        graph_version=settings.meta_api_version,
    )


@router.post("/whatsapp/embedded-signup", response_model=ConnectionOut)
async def whatsapp_embedded_signup(
    body: EmbeddedSignupBody, current_user: CurrentUserDep, db: DbDep
) -> ConnectionOut:
    """
    Finish connecting a business's WhatsApp account.

    The account stays theirs throughout. We hold only the authorisation they
    granted, and DELETE /channels/whatsapp gives it back.
    """
    business_id = current_user.business

    # Six digits, ours to choose, and it becomes the number's two-step
    # verification PIN - so it is stored encrypted. Re-registering this number
    # later needs this exact value, and there is no way to read it back from
    # Meta.
    pin = f"{secrets.randbelow(1_000_000):06d}"

    try:
        result = await signup.complete_signup(body.code, pin=pin)
    except signup.SignupError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    existing = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == business_id,
            ChannelConnection.channel == Channel.whatsapp,
            ChannelConnection.external_account_id == result.phone_number_id,
        )
    )
    connection = existing.scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if connection is None:
        connection = ChannelConnection(
            business_id=business_id,
            channel=Channel.whatsapp,
            external_account_id=result.phone_number_id,
            connected_at=now,
        )
        db.add(connection)

    connection.display_name = result.waba_name
    connection.external_handle = result.display_phone_number
    connection.access_token = encrypt(result.access_token)
    connection.token_issued_at = now
    connection.token_expires_at = result.token_expires_at
    connection.token_refresh_failed_at = None
    connection.status = ConnectionStatus.active
    connection.webhook_subscribed = result.webhook_subscribed
    connection.number_registered = result.number_registered
    connection.extra = {
        **(connection.extra or {}),
        "waba_id": result.waba_id,
        "verified_name": result.verified_name,
        "quality_rating": result.quality_rating,
        # Encrypted: it is the number's two-step PIN, not a display value.
        "two_step_pin": encrypt(result.registration_pin or ""),
    }

    logger.info(
        "whatsapp connected business=%s waba=%s number=%s expires=%s",
        business_id,
        result.waba_id,
        result.phone_number_id,
        result.token_expires_at,
    )

    return ConnectionOut(
        connected=True,
        channel="whatsapp",
        waba_id=result.waba_id,
        waba_name=result.waba_name,
        phone_number_id=result.phone_number_id,
        display_phone_number=result.display_phone_number,
        verified_name=result.verified_name,
        quality_rating=result.quality_rating,
        webhook_subscribed=result.webhook_subscribed,
        number_registered=result.number_registered,
        token_expires_at=(
            result.token_expires_at.isoformat() if result.token_expires_at else None
        ),
        graph_calls=[GraphCallOut(**asdict(c)) for c in result.calls],
    )


@router.get("/")
async def list_channels(current_user: CurrentUserDep, db: DbDep) -> list[dict]:
    """Every channel this business has connected, and whether it is healthy."""
    result = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == current_user.business
        )
    )
    return [
        {
            "id": str(c.id),
            "channel": c.channel.value if hasattr(c.channel, "value") else c.channel,
            "handle": c.external_handle,
            # Voice connections have no external_handle - the number Krova
            # actually calls it by lives in external_account_id instead, and
            # a voice UI showing "which number is this" needs it.
            "external_account_id": c.external_account_id,
            "display_name": c.display_name,
            "status": c.status.value if hasattr(c.status, "value") else c.status,
            "webhook_subscribed": c.webhook_subscribed,
            "number_registered": c.number_registered,
            "connected_at": c.connected_at.isoformat() if c.connected_at else None,
            "token_expires_at": (
                c.token_expires_at.isoformat() if c.token_expires_at else None
            ),
            # What the account actually is, so a business returning to the
            # onboarding screen sees its details rather than a row of dashes.
            # These are read back from what signup stored, never re-fetched -
            # a page load should not cost a Graph call.
            "waba_id": (c.extra or {}).get("waba_id"),
            "verified_name": (c.extra or {}).get("verified_name"),
            "quality_rating": (c.extra or {}).get("quality_rating"),
        }
        for c in result.scalars().all()
    ]


@router.delete("/whatsapp", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_whatsapp(current_user: CurrentUserDep, db: DbDep) -> None:
    """
    Give the account back.

    The stored token is destroyed rather than kept against a possible
    reconnection - holding a credential after being told to stop is exactly
    what we tell businesses we do not do.
    """
    result = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == current_user.business,
            ChannelConnection.channel == Channel.whatsapp,
        )
    )
    for connection in result.scalars().all():
        connection.access_token = None
        connection.refresh_token = None
        connection.status = ConnectionStatus.disconnected
        connection.webhook_subscribed = False
        connection.extra = {
            k: v for k, v in (connection.extra or {}).items() if k != "two_step_pin"
        }
        logger.info(
            "whatsapp disconnected business=%s number=%s",
            current_user.business,
            connection.external_account_id,
        )
