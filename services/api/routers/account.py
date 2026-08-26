"""
The client's WhatsApp account: profile, health, verification.

Everything a business would otherwise leave the platform to do in WhatsApp
Manager.
"""

import uuid
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.auth.encryption import decrypt, encrypt
from shared.channels.whatsapp import account as meta
from shared.db.models import Channel, ChannelConnection, ConnectionStatus
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/account/whatsapp", tags=["account"])


class ProfileOut(BaseModel):
    about: str | None
    address: str | None
    description: str | None
    email: str | None
    websites: list[str] | None
    vertical: str | None
    vertical_label: str | None
    profile_picture_url: str | None


class ProfileIn(BaseModel):
    about: str | None = Field(default=None, max_length=meta.MAX_ABOUT)
    address: str | None = Field(default=None, max_length=meta.MAX_ADDRESS)
    description: str | None = Field(default=None, max_length=meta.MAX_DESCRIPTION)
    email: str | None = Field(default=None, max_length=meta.MAX_EMAIL)
    websites: list[str] | None = Field(default=None, max_length=meta.MAX_WEBSITES)
    vertical: str | None = None


class HealthOut(BaseModel):
    phone_number_id: str
    display_phone_number: str | None
    verified_name: str | None
    quality_rating: str | None
    messaging_limit_tier: str | None
    daily_recipient_limit: int | None
    status: str | None
    code_verification_status: str | None
    name_status: str | None
    throughput_level: str | None
    account_mode: str | None
    is_official_business_account: bool
    healthy: bool
    warnings: list[str]


class BlockerOut(BaseModel):
    entity: str
    state: str
    code: int | None
    message: str
    fix: str | None


class ReadinessOut(BaseModel):
    ready: bool
    can_send: str
    needs_payment_method: bool
    action_required: str | None
    billing_url: str | None
    blockers: list[BlockerOut]
    notes: list[str]


class CodeRequest(BaseModel):
    method: Literal["SMS", "VOICE"] = "SMS"
    language: str = "en"


class CodeSubmit(BaseModel):
    code: str = Field(min_length=3, max_length=12)


class PinBody(BaseModel):
    pin: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


async def _connection(business_id: uuid.UUID, db: DbDep) -> ChannelConnection:
    result = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == business_id,
            ChannelConnection.channel == Channel.whatsapp,
            ChannelConnection.status == ConnectionStatus.active,
        )
    )
    connection = result.scalars().first()
    if connection is None or not connection.access_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connect WhatsApp first",
        )
    return connection


def _client(connection: ChannelConnection) -> meta.AccountClient:
    return meta.AccountClient(
        decrypt(connection.access_token), connection.external_account_id
    )


def _handle(exc: meta.AccountError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/profile", response_model=ProfileOut)
async def get_profile(current_user: CurrentUserDep, db: DbDep) -> ProfileOut:
    """
    What customers see when they open the chat.

    Worth surfacing prominently: it is the most visible thing on the account
    and the easiest to leave wrong for months.
    """
    connection = await _connection(current_user.business, db)
    try:
        profile = await _client(connection).get_profile()
    except meta.AccountError as exc:
        raise _handle(exc) from exc

    return ProfileOut(
        about=profile.about,
        address=profile.address,
        description=profile.description,
        email=profile.email,
        websites=profile.websites,
        vertical=profile.vertical,
        vertical_label=meta.VERTICALS.get(profile.vertical or ""),
        profile_picture_url=profile.profile_picture_url,
    )


@router.post("/profile", response_model=ProfileOut)
async def update_profile(
    body: ProfileIn, current_user: CurrentUserDep, db: DbDep
) -> ProfileOut:
    connection = await _connection(current_user.business, db)
    client = _client(connection)
    try:
        await client.update_profile(
            meta.BusinessProfile(
                about=body.about,
                address=body.address,
                description=body.description,
                email=body.email,
                websites=body.websites,
                vertical=body.vertical,
            )
        )
        profile = await client.get_profile()
    except meta.AccountError as exc:
        raise _handle(exc) from exc

    return ProfileOut(
        about=profile.about,
        address=profile.address,
        description=profile.description,
        email=profile.email,
        websites=profile.websites,
        vertical=profile.vertical,
        vertical_label=meta.VERTICALS.get(profile.vertical or ""),
        profile_picture_url=profile.profile_picture_url,
    )


@router.get("/verticals")
async def list_verticals(current_user: CurrentUserDep) -> list[dict]:
    """Meta's business categories, for the profile form."""
    return [{"value": k, "label": v} for k, v in sorted(meta.VERTICALS.items(),
                                                        key=lambda kv: kv[1])]


@router.get("/health", response_model=HealthOut)
async def number_health(current_user: CurrentUserDep, db: DbDep) -> HealthOut:
    """
    The state of the number, with the problems named.

    Quality rating and messaging tier are the two that decide whether a
    business can keep operating. Quality falls first, Meta restricts second,
    and messages visibly stop third — so this exists to catch the first step.
    """
    connection = await _connection(current_user.business, db)
    try:
        health = await _client(connection).health()
    except meta.AccountError as exc:
        raise _handle(exc) from exc

    warnings: list[str] = []
    if health.quality_rating == "YELLOW":
        warnings.append(
            "Message quality has dropped. Meta lowers your sending limit if this continues."
        )
    elif health.quality_rating == "RED":
        warnings.append(
            "Message quality is poor. Your number is at risk of being restricted."
        )
    if health.status and health.status != "CONNECTED":
        warnings.append(f"This number is {health.status.lower()} and cannot send.")
    if health.code_verification_status == "EXPIRED":
        warnings.append(
            "The number's verification has expired. It still sends, but "
            "re-registering will require a fresh code."
        )
    if health.messaging_limit_tier == "TIER_250":
        warnings.append(
            "You can message 250 new people a day. This rises automatically as "
            "you send quality messages."
        )
    if not health.is_official_business_account:
        warnings.append(
            "Not a verified business account — customers see your number, not a "
            "verified name badge."
        )

    return HealthOut(
        phone_number_id=health.phone_number_id,
        display_phone_number=health.display_phone_number,
        verified_name=health.verified_name,
        quality_rating=health.quality_rating,
        messaging_limit_tier=health.messaging_limit_tier,
        daily_recipient_limit=health.daily_limit,
        status=health.status,
        code_verification_status=health.code_verification_status,
        name_status=health.name_status,
        throughput_level=health.throughput_level,
        account_mode=health.account_mode,
        is_official_business_account=health.is_official_business_account,
        healthy=health.healthy,
        warnings=warnings,
    )


@router.get("/readiness", response_model=ReadinessOut)
async def readiness(current_user: CurrentUserDep, db: DbDep) -> ReadinessOut:
    """
    Can this client actually send yet?

    The question with no good answer without it. A client finishes onboarding,
    every tick in Krova is green, they send a message and nothing arrives -
    because Meta requires a payment method on their own WABA, and there is no
    API for us to add one or even read whether one exists.

    health_status is the only signal Meta gives, so this reads it, names the
    blocker in their words, and points the client at the page that fixes it.
    """
    connection = await _connection(current_user.business, db)
    try:
        state = await _client(connection).readiness()
    except meta.AccountError as exc:
        raise _handle(exc) from exc

    waba_id = (connection.extra or {}).get("waba_id")
    billing_url = (
        "https://business.facebook.com/billing_hub/accounts/details/"
        f"?asset_id={waba_id}"
        if waba_id and state.needs_payment_method
        else None
    )

    action: str | None = None
    if state.needs_payment_method:
        action = (
            "Add a payment method to your WhatsApp Business account. Meta bills "
            "you directly for messages - Krova adds no charge - and nothing will "
            "deliver until a payment method is on file."
        )
    elif state.can_send == "BLOCKED":
        action = "Meta has blocked sending on this account. See the details below."
    elif state.can_send == "LIMITED":
        action = "You can send, but with restrictions. See the details below."

    return ReadinessOut(
        ready=state.ready,
        can_send=state.can_send,
        needs_payment_method=state.needs_payment_method,
        action_required=action,
        billing_url=billing_url,
        # asdict, not vars: these are slots dataclasses and have no __dict__.
        blockers=[BlockerOut(**asdict(b)) for b in state.blockers],
        notes=state.notes,
    )


@router.post("/request-code", status_code=status.HTTP_202_ACCEPTED)
async def request_code(
    body: CodeRequest, current_user: CurrentUserDep, db: DbDep
) -> dict:
    """
    Send a verification code to the number.

    The code goes to the phone itself, so whoever holds it has to be present.
    """
    connection = await _connection(current_user.business, db)
    try:
        await _client(connection).request_verification_code(
            method=body.method, language=body.language
        )
    except meta.AccountError as exc:
        raise _handle(exc) from exc
    return {
        "sent": True,
        "method": body.method,
        "detail": f"A code has been sent to {connection.external_handle} by "
        f"{'SMS' if body.method == 'SMS' else 'voice call'}.",
    }


@router.post("/verify-code")
async def verify_code(
    body: CodeSubmit, current_user: CurrentUserDep, db: DbDep
) -> dict:
    connection = await _connection(current_user.business, db)
    try:
        await _client(connection).verify_code(body.code)
    except meta.AccountError as exc:
        raise _handle(exc) from exc
    return {"verified": True}


@router.post("/two-step-pin")
async def set_pin(body: PinBody, current_user: CurrentUserDep, db: DbDep) -> dict:
    """
    Set the number's two-step verification PIN.

    Stored encrypted because re-registering this number needs this exact
    value and Meta will not tell us what it is.
    """
    connection = await _connection(current_user.business, db)
    try:
        await _client(connection).set_two_step_pin(body.pin)
    except meta.AccountError as exc:
        raise _handle(exc) from exc

    connection.extra = {**(connection.extra or {}), "two_step_pin": encrypt(body.pin)}
    logger.info("two-step PIN changed number=%s", connection.external_account_id)
    return {"updated": True}
