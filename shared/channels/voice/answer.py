"""
The Answer URL - the first thing Plivo calls when someone dials in.

One Answer URL and one Stream endpoint serve every business's calls - each
business owns a subaccount, but a number could be looked up only after
reading the (as yet unverified) request, which is the wrong order for a
signature check. Plivo's Ma-V3 header solves this: it is always signed with
Krova's own PARENT auth token regardless of which subaccount the call's
number belongs to, confirmed directly with Plivo support, so this shared
ingress point can verify every request the same way without first knowing
whose call it is. Per-business subaccount tokens are still what every
outbound Plivo API call for that business uses - just not this check.
"""

import json

from fastapi import APIRouter, Header, Request, Response, status
from sqlalchemy import select

from shared.channels.voice import compliance
from shared.channels.voice.call_registry import remember
from shared.channels.voice.plivo_signature import InvalidSignature, verify
from shared.channels.voice.tenant import resolve
from shared.channels.voice.xml import (
    copilot_response,
    dial_response,
    hangup_response,
    stream_response,
)
from shared.config.settings import settings
from shared.db.models import VoiceProvisioning
from shared.db.session import AsyncSessionLocal
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.post("/voice/answer")
async def answer(
    request: Request,
    x_plivo_signature_ma_v3: str | None = Header(default=None),
    x_plivo_signature_v3_nonce: str | None = Header(default=None),
) -> Response:
    """
    Return the XML that opens a bidirectional stream for this call.

    Plivo sends call details as form-encoded POST body, not JSON - a
    telephony-API convention this endpoint has to match rather than choose.
    """
    body = await request.form()
    params = {k: str(v) for k, v in body.items()}

    try:
        verify(
            uri=f"{settings.public_base_url.rstrip('/')}/voice/answer",
            signature=x_plivo_signature_ma_v3,
            nonce=x_plivo_signature_v3_nonce,
            method="POST",
            params=params,
        )
    except InvalidSignature:
        logger.warning("rejected /voice/answer - bad plivo signature")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    to_number = body.get("To") or body.get("to")
    from_number = body.get("From") or body.get("from")
    call_uuid = body.get("CallUUID") or body.get("callId")

    async with AsyncSessionLocal() as db:
        route = await resolve(str(to_number or ""), db)

    if route is None:
        logger.warning("answer webhook for unrecognised number %s", to_number)
        return Response(
            content=hangup_response("number not connected to any business"),
            media_type="application/xml",
        )

    if call_uuid:
        remember(
            str(call_uuid),
            to_number=str(to_number or ""),
            from_number=str(from_number or ""),
        )

    # Live copilot mode: a human's phone rings directly, never the AI's
    # voice - copilot_mode with no staff_phone_number configured is treated
    # as not-opted-in rather than an error, so a half-finished setting can
    # never silently ring nobody.
    if route.copilot_mode and route.staff_phone_number:
        ws_url = f"{settings.public_base_url.replace('https://', 'wss://')}/voice/copilot-stream"
        return Response(
            content=copilot_response(
                ws_url,
                f"+{route.staff_phone_number}",
                status_callback_url=f"{settings.public_base_url}/voice/status",
            ),
            media_type="application/xml",
        )

    ws_url = f"{settings.public_base_url.replace('https://', 'wss://')}/voice/stream"
    return Response(
        content=stream_response(
            ws_url,
            status_callback_url=f"{settings.public_base_url}/voice/status",
        ),
        media_type="application/xml",
    )


@router.post("/voice/transfer-xml")
async def transfer_xml(
    request: Request,
    number: str,
    x_plivo_signature_ma_v3: str | None = Header(default=None),
    x_plivo_signature_v3_nonce: str | None = Header(default=None),
) -> Response:
    """
    Fetched by Plivo mid-call once plivo_client.transfer_call() has asked
    it to redirect a live call's A-leg here. The destination number
    travels in the URL's own query string - decided by us the moment the
    transfer is triggered, not stored anywhere new - so this endpoint
    needs no lookup at all, just the same signature check every other
    Plivo webhook here does. Signing over the raw received query string
    (request.url.query) rather than a rebuilt one means re-encoding the
    number here can never mismatch what Plivo actually signed.
    """
    body = await request.form()
    params = {k: str(v) for k, v in body.items()}

    try:
        verify(
            uri=f"{settings.public_base_url.rstrip('/')}/voice/transfer-xml?{request.url.query}",
            signature=x_plivo_signature_ma_v3,
            nonce=x_plivo_signature_v3_nonce,
            method="POST",
            params=params,
        )
    except InvalidSignature:
        logger.warning("rejected /voice/transfer-xml - bad plivo signature")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    return Response(content=dial_response(number), media_type="application/xml")


@router.post("/voice/status")
async def status_callback(request: Request) -> dict:
    """
    Plivo's out-of-band call status updates.

    Logged rather than acted on for now - duration and hangup cause are
    written from the WebSocket side, where the call actually lived.
    """
    body = await request.form()
    logger.info("voice status callback: %s", dict(body))
    return {"received": True}


@router.post("/voice/compliance-status")
async def compliance_status_callback(request: Request) -> dict:
    """
    Plivo pushing a KYC decision, instead of a business polling for it.

    ComplianceApplication is a parent-account resource (Krova submits every
    business's KYC from its own account), so this is verified like every
    other shared, business-agnostic webhook - Ma-V3, the parent token. The
    payload itself only tells us *that* something changed; the actual
    decision is re-fetched from Plivo rather than trusted from the body, so
    an unconfirmed signing detail here (JSON body vs the form-encoded POSTs
    the rest of this codebase verifies) cannot cause a wrong status to be
    written from a malformed or unverifiable field.
    """
    raw = await request.body()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {}

    try:
        verify(
            uri=f"{settings.public_base_url.rstrip('/')}/voice/compliance-status",
            signature=request.headers.get("x-plivo-signature-ma-v3")
            or request.headers.get("x-plivo-signature-v3"),
            nonce=request.headers.get("x-plivo-signature-v3-nonce"),
            method="POST",
            params=payload if isinstance(payload, dict) else {},
        )
    except InvalidSignature:
        logger.warning("rejected /voice/compliance-status - bad plivo signature")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    application_id = payload.get("compliance_application_id") or payload.get("application_id")
    if not application_id:
        logger.warning("compliance-status callback with no application id: %s", payload)
        return {"received": True}

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(VoiceProvisioning).where(
                VoiceProvisioning.compliance_application_id == application_id
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            logger.warning("compliance-status callback for unknown application %s", application_id)
            return {"received": True}

        try:
            fresh = await compliance.get_application_status(application_id)
        except Exception:
            logger.exception("could not re-fetch compliance status for %s", application_id)
            return {"received": True}

        compliance.apply_status(
            row, fresh.get("status", ""), fresh.get("rejection_reason") or fresh.get("reason")
        )
        await db.commit()

    logger.info("compliance status synced via webhook application=%s", application_id)
    return {"received": True}
