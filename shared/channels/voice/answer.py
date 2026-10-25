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
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Header, Request, Response, status
from sqlalchemy import select

from shared.channels.voice import compliance, human_calls, missed_calls
from shared.channels.voice.call_registry import remember
from shared.channels.voice.plivo_signature import InvalidSignature, verify
from shared.channels.voice.tenant import resolve
from shared.channels.voice.xml import (
    callback_promise_response,
    copilot_response,
    dial_group_response,
    dial_response,
    dial_step_response,
    hangup_response,
    listen_stream_line,
    nobody_answered_response,
    speak_response,
    stream_response,
    voicemail_response,
)
from shared.config.settings import settings
from shared.db.models import Call, VoiceProvisioning
from shared.team import call_routing
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

    if route.use == "outbound":
        logger.info("call to an outgoing-only number %s - declined", route.label)
        return Response(
            content=speak_response("This number is used for outgoing calls only. Please call us on our main number."),
            media_type="application/xml",
        )

    # An empty wallet or unpaid number rent switches calls off - otherwise KROVA would be
    # paying for the call. (Businesses that never topped up have no wallet and are never stopped.)
    from shared.billing.guards import voice_calls_blocked

    async with AsyncSessionLocal() as db:
        blocked = await voice_calls_blocked(db, route.business_id)
    if blocked:
        logger.warning("voice call refused: plan paused, wallet empty or rent unpaid business=%s", route.business_id)
        return Response(
            content=speak_response("This number cannot take calls right now. Please try again later."),
            media_type="application/xml",
        )

    if call_uuid:
        remember(
            str(call_uuid),
            to_number=str(to_number or ""),
            from_number=str(from_number or ""),
        )

    # Live Copilot on a number with its own "who answers" list rings that list. The AI never speaks; a short
    # greeting does.
    direct = await _direct_to_people(route, str(from_number or ""))
    if direct is not None:
        return Response(content=direct, media_type="application/xml")

    # Live copilot mode: a human's phone rings directly, never the AI's
    # voice - copilot_mode with no staff_phone_number configured is treated
    # as not-opted-in rather than an error, so a half-finished setting can
    # never silently ring nobody.
    if route.copilot_mode and route.staff_phone_number:
        # copilot_live_suggestions is the separate, costed opt-in
        # (tenant.py's own docstring on it) - only fork the audio stream
        # to copilot_stream/_pump_suggestions when a business has
        # actually turned that on. Off (the default): greeting + ring +
        # connect, no AI involved once the call is answered, at no
        # ongoing AI cost.
        ws_url = (
            f"{settings.public_base_url.replace('https://', 'wss://')}/voice/copilot-stream"
            if route.copilot_live_suggestions
            else None
        )
        return Response(
            content=copilot_response(
                f"+{route.staff_phone_number}",
                websocket_url=ws_url,
                status_callback_url=f"{settings.public_base_url}/voice/status",
                # route.greeting is written for the AI to carry the call
                # on afterwards ("...how can I help you?") - wrong framing
                # here, where a human is about to pick up instead. A
                # short, business-named line instead, matching exactly
                # what Nikhil asked for: a greeting, then ringing, then an
                # employee answers.
                greeting=(
                    f"Thank you for calling {route.business_name}. "
                    "Our team will connect with you shortly."
                ),
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


async def _direct_to_people(route, caller: str) -> str | None:
    """
    The XML that puts a Live Copilot call straight through to this number's "who answers" list. None means: carry
    on as before (the AI answers, or the old one-number copilot).
    """
    from shared.channels.voice import line_profile
    from shared.db.models import CustomerIdentity, IdentityKind
    from shared.identity.normalise import InvalidIdentifier, normalise_phone

    profile = line_profile.read(route.extra)
    if not route.copilot_mode or not profile["pickup"]["levels"]:
        return None
    try:
        async with AsyncSessionLocal() as db:
            customer_id = None
            try:
                phone = normalise_phone(caller) if caller else None
            except InvalidIdentifier:
                phone = None
            if phone:
                customer_id = (await db.execute(
                    select(CustomerIdentity.customer_id).where(
                        CustomerIdentity.business_id == route.business_id,
                        CustomerIdentity.kind == IdentityKind.phone, CustomerIdentity.value == phone,
                    )
                )).scalar_one_or_none()
            targets = await line_profile.plan(
                db, business_id=route.business_id, extra=route.extra, customer_id=customer_id,
                fallback=route.staff_phone_number,
            )
    except Exception:  # noqa: BLE001 - a lookup failure must fall back to the AI answering, never a dead call
        logger.exception("could not work out who answers business=%s", route.business_id)
        return None
    if not targets:
        return None
    steps = call_routing.decode_plan(call_routing.encode_plan(targets))
    query = plan_query(targets, connection_id=route.connection_id, call_id=None, caller=caller, start=1)
    stream = None
    if route.copilot_live_suggestions:
        stream = listen_stream_line(
            f"{settings.public_base_url.replace('https://', 'wss://')}/voice/copilot-stream",
            f"{settings.public_base_url}/voice/status",
        )
    logger.info("copilot call put through to the list business=%s steps=%s", route.business_id, len(steps))
    return dial_group_response(
        [number for number, _ in steps[0].stops], action_url=_transfer_url(query), timeout=steps[0].ring_seconds,
        greeting=f"Thank you for calling {route.business_name}. Connecting you to our team.", stream=stream,
    )


@router.post("/voice/transfer-xml")
async def transfer_xml(
    request: Request,
    number: str | None = None,
    t: str | None = None,
    i: int = 0,
    call: uuid.UUID | None = None,
    p: str | None = None,
    c: uuid.UUID | None = None,
    frm: str | None = None,
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

    if p is not None:
        return await _walk_plan(p, i, call, c, frm, params)

    if t is None:
        # One fixed number (a phone-menu key that rings somewhere specific).
        if not number:
            return Response(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
        return Response(content=dial_response(number), media_type="application/xml")

    # A list of team members, tried one after another. First request: i == 0, no DialStatus.
    # After each attempt Plivo posts back here with the result and the next index.
    stops = call_routing.decode(t)
    outcome = params.get("DialStatus")
    if outcome is not None and 0 < i <= len(stops):
        if outcome == "completed":
            number_answered, user_id = stops[i - 1]
            if call is not None:
                await _record_handler(call, user_id, str(params.get("DialBLegUUID") or ""))
            return Response(content=hangup_response("transfer completed"), media_type="application/xml")
    if i >= len(stops):
        return Response(content=nobody_answered_response(), media_type="application/xml")

    next_number, _ = stops[i]
    query = {"t": t, "i": i + 1}
    if call is not None:
        query["call"] = str(call)
    action_url = f"{settings.public_base_url.rstrip('/')}/voice/transfer-xml?{urlencode(query)}"
    return Response(
        content=dial_step_response(next_number, action_url=action_url, timeout=call_routing.RING_SECONDS),
        media_type="application/xml",
    )


def plan_query(targets: list, *, connection_id: uuid.UUID, call_id: uuid.UUID | None, caller: str | None,
               start: int = 0) -> dict[str, str]:
    """The query string that carries a number's "who answers" steps through Plivo, one step per request."""
    query = {"p": call_routing.encode_plan(targets), "i": str(start), "c": str(connection_id)}
    if call_id is not None:
        query["call"] = str(call_id)
    if caller:
        query["frm"] = "".join(ch for ch in caller if ch.isdigit())[:15]
    return query


def _transfer_url(query: dict) -> str:
    return f"{settings.public_base_url.rstrip('/')}/voice/transfer-xml?{urlencode(query)}"


async def _walk_plan(
    p: str, i: int, call: uuid.UUID | None, c: uuid.UUID | None, frm: str | None, params: dict,
) -> Response:
    """
    One step of a number's "who answers" list (shared/channels/voice/line_profile.py). `i` is the step to ring
    next; after each step Plivo posts back here with DialStatus. A step that rang several phones together does
    not say which one answered, so that is read from the answered leg's call record.
    """
    steps = call_routing.decode_plan(p)
    outcome = params.get("DialStatus")
    if outcome == "completed" and 0 < i <= len(steps):
        stops = steps[i - 1].stops
        b_leg = str(params.get("DialBLegUUID") or "")
        user_id = stops[0][1] if len(stops) == 1 else await _who_answered(c, b_leg, stops)
        if call is not None:
            await _record_handler(call, user_id, b_leg)
        return Response(content=hangup_response("transfer completed"), media_type="application/xml")
    if i >= len(steps):
        return await _nobody_answered(c, call, frm, steps)

    step = steps[i]
    query = {"p": p, "i": str(i + 1)}
    for key, value in (("c", c), ("call", call), ("frm", frm)):
        if value:
            query[key] = str(value)
    return Response(
        content=dial_group_response(
            [number for number, _ in step.stops], action_url=_transfer_url(query), timeout=step.ring_seconds,
        ),
        media_type="application/xml",
    )


async def _who_answered(connection_id: uuid.UUID | None, b_leg: str, stops: list) -> uuid.UUID | None:
    """Which of several phones rung together picked up - from the answered leg's own call record."""
    if not connection_id or not b_leg:
        return None
    from shared.auth.encryption import decrypt
    from shared.channels.voice import plivo_client
    from shared.db.models import ChannelConnection

    try:
        async with AsyncSessionLocal() as db:
            connection = await db.get(ChannelConnection, connection_id)
        auth_id = ((connection.extra or {}) if connection else {}).get("subaccount_auth_id")
        if not connection or not auth_id or not connection.access_token:
            return None
        record = await plivo_client.get_call_cdr(
            auth_id=auth_id, auth_token=decrypt(connection.access_token), call_uuid=b_leg,
        )
    except Exception:  # noqa: BLE001 - not knowing who answered must never break the call
        logger.exception("could not read who answered b_leg=%s", b_leg)
        return None
    answered = "".join(ch for ch in str((record or {}).get("to_number") or "") if ch.isdigit())
    return next((uid for number, uid in stops if answered and answered.endswith(number[-10:])), None)


async def _nobody_answered(
    connection_id: uuid.UUID | None, call_id: uuid.UUID | None, caller: str | None, steps: list,
) -> Response:
    """Every step was tried. Do what this number's owner chose: a callback promise, a voicemail, or an apology."""
    from shared.channels.voice import line_profile
    from shared.db.models import ChannelConnection

    profile = line_profile.read(None)
    if connection_id is not None:
        async with AsyncSessionLocal() as db:
            connection = await db.get(ChannelConnection, connection_id)
        if connection is not None:
            profile = line_profile.read(connection.extra)
    choice = profile["no_answer"]
    if choice == "apology" or connection_id is None:
        return Response(content=nobody_answered_response(), media_type="application/xml")
    level_one = [uid for step in steps[:1] for _, uid in step.stops if uid]
    if choice == "voicemail":
        query = {"c": str(connection_id)}
        if call_id is not None:
            query["call"] = str(call_id)
        if caller:
            query["frm"] = caller
        if level_one:
            query["u"] = ",".join(str(u) for u in level_one)
        url = f"{settings.public_base_url.rstrip('/')}/voice/voicemail?{urlencode(query)}"
        return Response(content=voicemail_response(action_url=url), media_type="application/xml")
    human_calls._detach(missed_calls.promise_callback(
        connection_id=connection_id, call_id=call_id, caller=caller, notify=level_one,
        hours=profile["callback_hours"],
    ))
    return Response(content=callback_promise_response(profile["callback_hours"]), media_type="application/xml")


@router.post("/voice/voicemail")
async def voicemail(
    request: Request,
    c: uuid.UUID,
    call: uuid.UUID | None = None,
    frm: str | None = None,
    u: str | None = None,
    x_plivo_signature_ma_v3: str | None = Header(default=None),
    x_plivo_signature_v3_nonce: str | None = Header(default=None),
) -> Response:
    """Plivo posts the message a caller left when nobody picked up. It goes on the Ledger as a callback to make."""
    body = await request.form()
    params = {k: str(v) for k, v in body.items()}
    try:
        verify(
            uri=f"{settings.public_base_url.rstrip('/')}/voice/voicemail?{request.url.query}",
            signature=x_plivo_signature_ma_v3,
            nonce=x_plivo_signature_v3_nonce,
            method="POST",
            params=params,
        )
    except InvalidSignature:
        logger.warning("rejected /voice/voicemail - bad plivo signature")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    from shared.channels.voice import line_profile
    from shared.db.models import ChannelConnection

    async with AsyncSessionLocal() as db:
        connection = await db.get(ChannelConnection, c)
    hours = line_profile.read(connection.extra if connection else None)["callback_hours"]
    notify = []
    for part in (u or "").split(","):
        try:
            notify.append(uuid.UUID(part))
        except ValueError:
            continue
    seconds = params.get("RecordingDuration") or "0"
    human_calls._detach(missed_calls.promise_callback(
        connection_id=c, call_id=call, caller=frm, notify=notify, hours=hours,
        recording_url=params.get("RecordUrl") or None,
        recording_seconds=int(seconds) if seconds.lstrip("-").isdigit() else 0,
    ))
    return Response(
        content=speak_response("Thank you. Your message has been recorded and someone from the team will call you back."),
        media_type="application/xml",
    )


@router.post("/voice/line-test-answer")
async def line_test_answer(
    request: Request,
    c: uuid.UUID,
    x_plivo_signature_ma_v3: str | None = Header(default=None),
    x_plivo_signature_v3_nonce: str | None = Header(default=None),
) -> Response:
    """The owner's "Test call" from the Voice page: a fixed line, no AI, then hang up."""
    body = await request.form()
    params = {k: str(v) for k, v in body.items()}
    try:
        verify(
            uri=f"{settings.public_base_url.rstrip('/')}/voice/line-test-answer?{request.url.query}",
            signature=x_plivo_signature_ma_v3,
            nonce=x_plivo_signature_v3_nonce,
            method="POST",
            params=params,
        )
    except InvalidSignature:
        logger.warning("rejected /voice/line-test-answer - bad plivo signature")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    from shared.channels.voice import numbers
    from shared.db.models import ChannelConnection

    async with AsyncSessionLocal() as db:
        connection = await db.get(ChannelConnection, c)
    label = numbers.label_of(connection) if connection else "KROVA"
    return Response(
        content=speak_response(
            f"This is a KROVA test call from your {label} line. If you can hear this, calls put through to this "
            "phone will reach you. Goodbye."
        ),
        media_type="application/xml",
    )


@router.post("/voice/line-test-hangup")
async def line_test_hangup(request: Request) -> dict:
    """Plivo needs a hangup URL; this only records how the test call ended, for support."""
    body = await request.form()
    logger.info(
        "line test call ended to=%s status=%s cause=%s", body.get("To"), body.get("CallStatus"),
        body.get("HangupCauseName") or body.get("HangupCause"),
    )
    return {"status": "ok"}


async def _record_handler(call_id: uuid.UUID, user_id: uuid.UUID | None, b_leg: str = "") -> None:
    """
    Remember who took the call (so they can be asked to write it up) and which Plivo leg
    carried it to them (so its cost reaches the wallet - nothing else records that leg).
    """
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(Call, call_id)
            if row is not None:
                if user_id is not None and row.handled_by_user_id is None:
                    row.handled_by_user_id = user_id
                if b_leg:
                    row.cost_breakdown = {**(row.cost_breakdown or {}), "b_leg_uuid": b_leg}
            await db.commit()
        if b_leg:
            human_calls._detach(human_calls.record_leg_costs(call_id, only_b_leg=True))
    except Exception:
        logger.exception("could not record who took call=%s", call_id)


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
