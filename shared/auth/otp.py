"""
Passwordless login/registration codes - two delivery channels, each tied
to exactly one identifier:

  email -> the code by email (shared/integrations/gmail_smtp.py)
  call  -> the code read aloud over a voice call, placed from Krova's own
           dedicated number (shared/channels/voice/otp_call.py) - never
           SMS. KROVA's own SMS sending has no working DLT registration
           (confirmed broken this project), and a voice call reuses
           infrastructure this codebase already has working, rather than
           adding a third messaging provider for one feature.

Either channel can register a brand-new account (shared/auth/service.py's
register_via_otp for email, register_via_phone_otp for phone - a
generated, never-shown password either way, same trick register_via_google
uses). A phone number can also be linked to an already-signed-in account
that registered by some other means (Settings - see User.phone's own
docstring) - request()/verify() are channel-agnostic either way; which of
login/register/add-phone a successful verify leads to is the caller's
decision, in services/api/routers/auth.py.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import encrypt
from shared.channels.voice import plivo_client
from shared.config.settings import settings
from shared.db.models import OtpCode
from shared.identity.normalise import InvalidIdentifier, normalise_phone
from shared.integrations import gmail_smtp
from shared.utils.logging import get_logger

logger = get_logger(__name__)

CODE_LENGTH = 6
EXPIRY = timedelta(minutes=10)
RESEND_COOLDOWN = timedelta(seconds=45)
MAX_ATTEMPTS = 5

CHANNELS = ("email", "call")


class OtpError(Exception):
    """Safe to show to the person as-is."""


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _generate_code() -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(CODE_LENGTH))


def normalise_destination(destination: str, channel: str) -> str:
    if channel not in CHANNELS:
        raise OtpError("Unknown verification channel")
    if channel == "email":
        return destination.strip().lower()
    try:
        return normalise_phone(destination)
    except InvalidIdentifier as exc:
        raise OtpError("That doesn't look like a valid phone number") from exc


async def _latest(destination: str, channel: str, db: AsyncSession) -> OtpCode | None:
    result = await db.execute(
        select(OtpCode)
        .where(OtpCode.destination == destination, OtpCode.channel == channel)
        .order_by(OtpCode.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def _send_email_code(destination: str, code: str) -> None:
    await gmail_smtp.send_email(
        to=destination,
        subject="Your KROVA verification code",
        body=(
            f"Your verification code is {code}.\n\n"
            "It expires in 10 minutes. If you didn't request this, you can "
            "safely ignore this email."
        ),
    )


async def _send_call_code(destination: str, otp_id: uuid.UUID) -> None:
    if not settings.otp_voice_from_number:
        raise OtpError("Phone verification is not configured on this server")
    base = settings.public_base_url.rstrip("/")
    try:
        await plivo_client.make_call(
            auth_id=settings.plivo_auth_id,
            auth_token=settings.plivo_auth_token,
            from_number=settings.otp_voice_from_number,
            to_number=destination,
            answer_url=f"{base}/voice/otp-answer?otp_id={otp_id}",
            hangup_url=f"{base}/voice/otp-hangup",
        )
    except plivo_client.PlivoError as exc:
        logger.error("could not place otp call to=%s: %s", destination, exc)
        raise OtpError("Could not place the verification call - please try again shortly") from exc


async def request(destination: str, channel: str, db: AsyncSession, *, purpose: str) -> None:
    """
    Generate and send one code.

    `purpose` ("login" | "register" | "add_phone") is logged only, for
    anyone reading these logs later - verify() below does not check it.
    The same code that logs someone in also completes a registration or
    links a phone, whichever services/api/routers/auth.py does next with
    a successful verify(); there is nothing channel-specific about the
    code itself to separate by purpose.
    """
    destination = normalise_destination(destination, channel)

    recent = await _latest(destination, channel, db)
    if recent is not None and recent.created_at > datetime.now(timezone.utc) - RESEND_COOLDOWN:
        raise OtpError("Please wait a moment before requesting another code")

    code = _generate_code()
    now = datetime.now(timezone.utc)
    otp = OtpCode(
        destination=destination,
        channel=channel,
        code_hash=_hash(code),
        code_enc=encrypt(code) if channel == "call" else None,
        expires_at=now + EXPIRY,
    )
    db.add(otp)
    await db.flush()

    try:
        if channel == "email":
            await _send_email_code(destination, code)
        else:
            await _send_call_code(destination, otp.id)
    except gmail_smtp.GmailSmtpError as exc:
        # The row stays even though sending failed - nothing can be
        # brute-forced off a code that never left this process, and the
        # cooldown above already governs how soon a retry can happen.
        logger.exception("could not send otp destination=%s channel=%s", destination, channel)
        raise OtpError("Could not send the verification code - please try again shortly") from exc

    logger.info("otp requested destination=%s channel=%s purpose=%s", destination, channel, purpose)


async def verify(destination: str, channel: str, code: str, db: AsyncSession) -> None:
    """
    Raises OtpError if the code is wrong, expired, or already used up on
    attempts; otherwise marks it consumed and returns. Proves only that
    destination+channel+code line up - what that means (log in, register,
    link a phone) is entirely the caller's decision afterwards.
    """
    destination = normalise_destination(destination, channel)
    otp = await _latest(destination, channel, db)

    if otp is None or otp.consumed_at is not None or otp.expires_at < datetime.now(timezone.utc):
        raise OtpError("That code is invalid or has expired")
    if otp.attempts >= MAX_ATTEMPTS:
        raise OtpError("Too many incorrect attempts - request a new code")

    if otp.code_hash != _hash(code.strip()):
        otp.attempts += 1
        raise OtpError("That code is incorrect")

    otp.consumed_at = datetime.now(timezone.utc)
