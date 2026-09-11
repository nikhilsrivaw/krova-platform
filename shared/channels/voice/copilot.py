"""
Live copilot mode: KROVA listens to a human-answered call and shows the
staff member real-time suggested talking points, instead of the AI
answering the call directly.

Two WebSockets meet here. `/voice/copilot-stream` is Plivo's listen-only
audio fork (see xml.py's copilot_response) - the same wire format and
signature check as relay.py's `/voice/stream`, but read-only: this module
never sends audio back, since KROVA never speaks in this mode.
`/voice/copilot-assist` is the browser's side - a staff member's dashboard
holds this open and receives whatever `/voice/copilot-stream` generates
for their business, via copilot_registry's in-process fan-out.

Deliberately out of scope here (see the project plan this shipped under):
feeding this call's transcript back through ingest() into the commitment
ledger, and writing a Call row the way relay.py does for AI-answered
calls. Speaker-separating a bridged two-party Dial'd call well enough to
attribute "customer said X" vs "staff said Y" is real additional
complexity worth solving once this core suggestion mechanism is proven
live, not before.
"""

import asyncio
import base64
import json
import uuid

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from shared.ai import context as agent_context
from shared.ai import copilot_suggest
from shared.auth.tokens import TokenError, decode_access_token
from shared.billing import usage
from shared.channels.voice import sarvam
from shared.channels.voice.call_registry import recall as recall_call
from shared.channels.voice.copilot_registry import publish, subscribe, unsubscribe
from shared.channels.voice.plivo_signature import InvalidSignature, verify
from shared.channels.voice.tenant import resolve
from shared.config.settings import settings
from shared.db.models import IdentityKind, UsageEventType
from shared.db.session import AsyncSessionLocal
from shared.identity import resolver as identity_resolver
from shared.identity.normalise import InvalidIdentifier
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


async def _connect_sarvam_stt():
    """
    Same tolerance relay.py's own _connect_sarvam has: the handshake
    occasionally takes longer than the 10s default on a real connection,
    confirmed there against live calls - one retry at a longer timeout
    rather than raising the default everywhere.
    """
    url = sarvam.stt_connect_url(language="auto")
    headers = sarvam.auth_headers()
    try:
        return await websockets.connect(url, additional_headers=headers, open_timeout=15)
    except TimeoutError:
        logger.warning("sarvam handshake slow on copilot stream, retrying once")
        return await websockets.connect(url, additional_headers=headers, open_timeout=20)


@router.websocket("/voice/copilot-stream")
async def copilot_stream(websocket: WebSocket) -> None:
    """
    The socket Plivo's listen-only <Stream> connects to for a copilot-mode
    call. Verified the same way relay.py's /voice/stream is - Plivo signs
    this upgrade with Krova's parent auth token regardless of which
    business's subaccount owns the number, so this shared endpoint can
    check every request the same way before knowing whose call it is.
    """
    try:
        verify(
            uri=f"http://{settings.public_base_url.split('://', 1)[-1].rstrip('/')}"
            "/voice/copilot-stream",
            signature=websocket.headers.get("x-plivo-signature-ma-v3"),
            nonce=websocket.headers.get("x-plivo-signature-v3-nonce"),
            method="GET",
        )
    except InvalidSignature:
        logger.warning("rejected copilot voice websocket upgrade - bad plivo signature")
        await websocket.close(code=4003)
        return

    await websocket.accept()

    stt_ws = None
    pump_task: asyncio.Task | None = None
    call_uuid = "unknown"

    try:
        while True:
            raw = await websocket.receive_text()
            message = json.loads(raw)
            event = message.get("event")

            if event == "start":
                start_info = message.get("start", {})
                call_uuid = (
                    start_info.get("callId") or start_info.get("streamId") or "unknown"
                )

                remembered = recall_call(call_uuid) or {}
                to_number = remembered.get("to")
                from_number = remembered.get("from")

                async with AsyncSessionLocal() as db:
                    route = await resolve(to_number or "", db)
                    if route is None:
                        logger.warning("copilot call to unrecognised number %s", to_number)
                        await websocket.close()
                        return
                    business_id = route.business_id

                    customer_id = None
                    try:
                        resolution = await identity_resolver.resolve(
                            business_id, IdentityKind.phone, from_number or "", db,
                        )
                        customer_id = resolution.customer.id
                    except InvalidIdentifier:
                        logger.warning("copilot call from unusable number %s", from_number)
                    await db.commit()

                logger.info(
                    "copilot call started business=%s call=%s", business_id, call_uuid
                )
                stt_ws = await _connect_sarvam_stt()
                pump_task = asyncio.create_task(
                    _pump_suggestions(stt_ws, business_id, customer_id, call_uuid)
                )

            elif event == "media":
                if stt_ws is None:
                    continue
                payload = (message.get("media") or {}).get("payload")
                if payload:
                    await stt_ws.send(sarvam.stt_audio_frame(base64.b64decode(payload)))

            elif event in ("stop", "end"):
                logger.info("copilot call ended call=%s", call_uuid)
                break

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("copilot voice relay error call=%s", call_uuid)
    finally:
        if pump_task is not None:
            pump_task.cancel()
        if stt_ws is not None:
            try:
                await stt_ws.close()
            except Exception:
                pass


async def _pump_suggestions(
    stt_ws, business_id: uuid.UUID, customer_id: uuid.UUID | None, call_uuid: str
) -> None:
    """Turn final transcripts into suggestions, published for whichever dashboard is watching."""
    if customer_id is None:
        logger.warning("copilot call=%s has no resolved customer, no suggestions possible", call_uuid)
        return

    try:
        async for transcript in sarvam.stream_transcripts(stt_ws):
            if not transcript.is_final:
                continue

            async with AsyncSessionLocal() as db:
                context = await agent_context.build(business_id, customer_id, db)
                result = await copilot_suggest.suggest(context)
                if result.cost_paise:
                    usage.record(
                        business_id=business_id,
                        event_type=UsageEventType.ai_reply_generated,
                        channel="voice",
                        quantity=1,
                        unit="call",
                        krova_cost_paise=result.cost_paise,
                        source_type="call",
                        db=db,
                    )
                await db.commit()

            if result.text:
                publish(business_id, {"call_uuid": call_uuid, "suggestion": result.text})
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception:
        logger.exception("copilot suggestion pump failed call=%s", call_uuid)


@router.websocket("/voice/copilot-assist")
async def copilot_assist(websocket: WebSocket, token: str) -> None:
    """
    The browser's side - a staff member's live-assist dashboard page holds
    this open and receives whatever /voice/copilot-stream generates for
    their business. Authenticated with the same access token the rest of
    the dashboard already holds, not a Plivo signature - this is a KROVA
    user's own session, not a telephony callback.
    """
    try:
        claims = decode_access_token(token)
        business_id_raw = claims.get("biz")
        if not business_id_raw:
            raise TokenError("Token has no business")
        business_id = uuid.UUID(business_id_raw)
    except (TokenError, ValueError):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    queue = subscribe(business_id)
    try:
        while True:
            message = await queue.get()
            await websocket.send_text(json.dumps(message))
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("copilot assist socket error business=%s", business_id)
    finally:
        unsubscribe(business_id, queue)
