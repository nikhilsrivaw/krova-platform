"""
The one voice call shape in this codebase with no AI in it at all: read a
login/registration OTP aloud, then hang up (shared/auth/otp.py places the
call; this is just the Answer URL it points at).

No <Stream>, no Sarvam, no Claude - shared/channels/voice/xml.py's
speak_response() is the whole reply. Verified the same way every other
outbound answer webhook already is (Ma-V3, Krova's own parent token,
signed over the exact query string Plivo received) - see outbound.py's
outbound_answer for why Ma-V3 applies regardless of which number placed
the call.
"""

import uuid

from fastapi import APIRouter, Header, Request, Response, status

from shared.auth.encryption import DecryptionError, decrypt
from shared.channels.voice.plivo_signature import InvalidSignature, verify
from shared.channels.voice.xml import hangup_response, speak_response
from shared.config.settings import settings
from shared.db.models import OtpCode
from shared.db.session import AsyncSessionLocal
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


def _spoken_digits(code: str) -> str:
    """'123456' -> '1. 2. 3. 4. 5. 6.' - punctuation-spaced so any TTS
    voice (not just a Polly one that understands SSML say-as) reads each
    digit clearly rather than as a six-digit number."""
    return ". ".join(code) + "."


@router.post("/voice/otp-answer")
async def otp_answer(
    otp_id: uuid.UUID,
    request: Request,
    x_plivo_signature_ma_v3: str | None = Header(default=None),
    x_plivo_signature_v3_nonce: str | None = Header(default=None),
) -> Response:
    body = await request.form()
    params = {k: str(v) for k, v in body.items()}

    try:
        verify(
            uri=f"{settings.public_base_url.rstrip('/')}/voice/otp-answer?{request.url.query}",
            signature=x_plivo_signature_ma_v3,
            nonce=x_plivo_signature_v3_nonce,
            method="POST",
            params=params,
        )
    except InvalidSignature:
        logger.warning("rejected /voice/otp-answer - bad plivo signature")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    async with AsyncSessionLocal() as db:
        otp = await db.get(OtpCode, otp_id)

    if otp is None or otp.consumed_at is not None or not otp.code_enc:
        logger.warning("otp-answer for unknown/consumed/codeless otp_id=%s", otp_id)
        return Response(content=hangup_response("code no longer valid"), media_type="application/xml")

    try:
        code = decrypt(otp.code_enc)
    except DecryptionError:
        logger.error("could not decrypt otp code for otp_id=%s", otp_id)
        return Response(content=hangup_response("could not read code"), media_type="application/xml")

    spoken = _spoken_digits(code)
    text = (
        f"Your KROVA verification code is {spoken} I repeat, your code is {spoken} "
        "This code expires in ten minutes."
    )
    return Response(content=speak_response(text), media_type="application/xml")


@router.post("/voice/otp-hangup")
async def otp_hangup(request: Request) -> dict:
    """
    Nothing to persist - a failed or unanswered OTP call just leaves the
    code unsent, and the person retries past otp.py's own resend cooldown.
    Exists because Plivo's Call API requires a hangup_url.

    What it does log is Plivo's own account of how the call ended. Without
    this, "the OTP call never arrived" was undiagnosable from our side:
    the API logged a successful request, then a hangup, and nothing in
    between to say whether the phone never rang, was declined, or was
    blocked before connecting. Plivo sends CallStatus / HangupCause /
    HangupCauseCode / Duration on every ended call.
    """
    body = await request.form()
    logger.info(
        "otp call ended otp_id=%s to=%s status=%s cause=%s code=%s duration=%s",
        request.query_params.get("otp_id"),
        body.get("To"),
        body.get("CallStatus"),
        body.get("HangupCauseName") or body.get("HangupCause"),
        body.get("HangupCauseCode"),
        body.get("Duration"),
    )
    return {"status": "ok"}
