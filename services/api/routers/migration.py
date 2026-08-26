"""
Bringing a number across from another provider.

Four steps, each its own endpoint, because the middle one needs a human
holding the phone. A single "migrate" call could not work: Meta sends a code
by SMS and someone has to read it.

The readiness check comes first and is the reason this flow succeeds or
wastes a client's afternoon. Two-step verification has to be switched off at
the old provider, and only they can do it — so it is stated before anything
starts rather than discovered at step one.
"""

import secrets
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.auth.encryption import decrypt, encrypt
from shared.channels.whatsapp import migration as meta
from shared.db.models import Channel, ChannelConnection, ConnectionStatus
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/migration/whatsapp", tags=["migration"])


class ReadinessOut(BaseModel):
    can_start: bool
    checks: list[dict]
    what_carries_over: list[str]


class StartIn(BaseModel):
    phone: str = Field(min_length=6, max_length=24)


class StartOut(BaseModel):
    phone_number_id: str
    display_phone_number: str
    next_step: str


class CodeIn(BaseModel):
    phone_number_id: str
    method: Literal["SMS", "VOICE"] = "SMS"
    language: str = "en"


class VerifyIn(BaseModel):
    phone_number_id: str
    code: str = Field(min_length=3, max_length=12)


class FinishIn(BaseModel):
    phone_number_id: str
    display_phone_number: str | None = None


class FinishOut(BaseModel):
    connected: bool
    phone_number_id: str
    display_phone_number: str | None
    note: str


async def _waba(business_id: uuid.UUID, db: DbDep) -> tuple[ChannelConnection, str]:
    """The client's own WABA — a number migrates onto their account, not ours."""
    result = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == business_id,
            ChannelConnection.channel == Channel.whatsapp,
        )
    )
    connection = result.scalars().first()
    if connection is None or not connection.access_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Connect your WhatsApp Business Account first. A number is "
                "migrated onto an account you already own."
            ),
        )
    waba_id = (connection.extra or {}).get("waba_id")
    if not waba_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This connection is incomplete. Please reconnect WhatsApp.",
        )
    return connection, waba_id


def _client(connection: ChannelConnection, waba_id: str) -> meta.MigrationClient:
    return meta.MigrationClient(decrypt(connection.access_token), waba_id)


def _fail(exc: meta.MigrationError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/readiness", response_model=ReadinessOut)
async def readiness(current_user: CurrentUserDep, db: DbDep) -> ReadinessOut:
    """
    What has to be true before starting, and who fixes each one.

    Worth reading before anything else: the step that blocks most migrations
    is one only the old provider can perform.
    """
    connection, waba_id = await _waba(current_user.business, db)
    state = await _client(connection, waba_id).readiness()

    return ReadinessOut(
        can_start=state.can_start,
        checks=[asdict(c) for c in state.checks],
        what_carries_over=[
            "Your display name — customers see the same business",
            "Your quality rating — no rebuilding it from scratch",
            "Your messaging limit tier — a number on 10,000/day stays there",
            "Official Business Account status, if you have it",
            "High-quality approved templates, auto-approved on arrival",
        ],
    )


@router.post("/start", response_model=StartOut)
async def start(body: StartIn, current_user: CurrentUserDep, db: DbDep) -> StartOut:
    """
    Claim the number onto the client's WABA.

    This is where two-step verification bites. Meta's error for it is opaque,
    so it is translated before the client sees it.
    """
    connection, waba_id = await _waba(current_user.business, db)
    try:
        result = await _client(connection, waba_id).start(body.phone)
    except meta.MigrationError as exc:
        raise _fail(exc) from exc

    logger.info(
        "migration claimed business=%s number=%s",
        current_user.business,
        result.display_phone_number,
    )
    return StartOut(
        phone_number_id=result.phone_number_id,
        display_phone_number=result.display_phone_number,
        next_step=(
            "Send a verification code to this number. Someone needs to be "
            "holding the phone."
        ),
    )


@router.post("/request-code", status_code=status.HTTP_202_ACCEPTED)
async def request_code(body: CodeIn, current_user: CurrentUserDep, db: DbDep) -> dict:
    connection, waba_id = await _waba(current_user.business, db)
    try:
        await _client(connection, waba_id).request_code(
            body.phone_number_id, method=body.method, language=body.language
        )
    except meta.MigrationError as exc:
        raise _fail(exc) from exc
    return {
        "sent": True,
        "method": body.method,
        "detail": (
            "A six-digit code is on its way by "
            f"{'SMS' if body.method == 'SMS' else 'voice call'}."
        ),
    }


@router.post("/verify-code")
async def verify_code(body: VerifyIn, current_user: CurrentUserDep, db: DbDep) -> dict:
    connection, waba_id = await _waba(current_user.business, db)
    try:
        await _client(connection, waba_id).verify_code(body.phone_number_id, body.code)
    except meta.MigrationError as exc:
        raise _fail(exc) from exc
    return {"verified": True, "next_step": "Finish the migration to start sending."}


@router.post("/finish", response_model=FinishOut)
async def finish(body: FinishIn, current_user: CurrentUserDep, db: DbDep) -> FinishOut:
    """
    Register the number and store the connection.

    A fresh two-step PIN is generated and kept encrypted. Re-registering this
    number later needs that exact value, and Meta will not tell us what it is.
    """
    connection, waba_id = await _waba(current_user.business, db)
    pin = f"{secrets.randbelow(1_000_000):06d}"

    try:
        await _client(connection, waba_id).register(body.phone_number_id, pin)
    except meta.MigrationError as exc:
        raise _fail(exc) from exc

    now = datetime.now(timezone.utc)
    existing = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == current_user.business,
            ChannelConnection.channel == Channel.whatsapp,
            ChannelConnection.external_account_id == body.phone_number_id,
        )
    )
    migrated = existing.scalars().first()
    if migrated is None:
        migrated = ChannelConnection(
            business_id=current_user.business,
            channel=Channel.whatsapp,
            external_account_id=body.phone_number_id,
            connected_at=now,
            # Reuse the WABA's token: the number now lives on the same account
            # the client already authorised us against.
            access_token=connection.access_token,
            token_issued_at=connection.token_issued_at,
            token_expires_at=connection.token_expires_at,
        )
        db.add(migrated)

    migrated.external_handle = body.display_phone_number
    migrated.status = ConnectionStatus.active
    migrated.number_registered = True
    # subscribed_apps is set on the WABA, not per number, so a migrated number
    # inherits the subscription the original connection already made.
    migrated.webhook_subscribed = connection.webhook_subscribed
    migrated.extra = {
        **(migrated.extra or {}),
        "waba_id": waba_id,
        "two_step_pin": encrypt(pin),
        "migrated_at": now.isoformat(),
        "migrated_from": "another provider",
    }

    logger.info(
        "migration finished business=%s number=%s",
        current_user.business,
        body.phone_number_id,
    )
    return FinishOut(
        connected=True,
        phone_number_id=body.phone_number_id,
        display_phone_number=body.display_phone_number,
        note=(
            "The number is live on Krova. Your quality rating, messaging limit "
            "and approved templates came across with it."
        ),
    )
