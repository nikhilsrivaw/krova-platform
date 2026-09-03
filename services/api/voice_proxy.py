"""
Forwarding the voice channel's HTTP and WebSocket traffic to its own process.

`services/voice/main.py` exists precisely so a phone call's WebSocket - held
open for the length of the call - never shares a deploy cycle with the HTTP
API's own restarts. But Plivo's Application only points at one public
domain, and the free ngrok plan gives exactly one. This module is the
compromise: the API process, which owns the one exposed domain, is a thin
forwarder onto the real voice process on its own port - two genuinely
separate processes (a voice-service crash or restart no longer takes the
API down, and vice versa) sharing one externally-visible address rather than
one process pretending to be two.

Move this to two real domains (a paid ngrok plan, or a proper deployment)
and this file simply stops being imported - services/voice/main.py needs no
changes either way, since it has always been a complete, standalone app.

Plivo's signature is checked HERE, before anything is proxied or accepted -
not left solely to the voice service downstream - because a reverse proxy
that accepts an unverified WebSocket upgrade before dropping it internally
already did the expensive, abusable part (holding a socket open) that the
check exists to prevent. The voice service checks it again on its own
connection; that second check is redundant on genuine traffic but is what
keeps the voice service correct as a standalone app if it's ever reached
directly, without trusting this proxy to always sit in front of it.
"""

import asyncio

import httpx
import websockets
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect

from shared.channels.voice.plivo_signature import InvalidSignature, verify
from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()

_VOICE_HTTP_BASE = "http://127.0.0.1:8100"
_VOICE_WS_BASE = "ws://127.0.0.1:8100"
_PROXY_TIMEOUT = 20.0


async def _proxy_http(request: Request, path: str) -> Response:
    body = await request.body()
    forwarded_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")
    }
    async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT) as client:
        try:
            upstream = await client.request(
                request.method,
                f"{_VOICE_HTTP_BASE}{path}",
                content=body,
                headers=forwarded_headers,
            )
        except httpx.RequestError:
            logger.exception("voice service unreachable proxying %s", path)
            return Response(status_code=502, content=b"voice service unreachable")

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={
            k: v
            for k, v in upstream.headers.items()
            if k.lower() not in ("content-length", "transfer-encoding", "connection")
        },
    )


@router.post("/voice/answer")
async def proxy_voice_answer(request: Request) -> Response:
    return await _proxy_http(request, "/voice/answer")


@router.post("/voice/status")
async def proxy_voice_status(request: Request) -> Response:
    return await _proxy_http(request, "/voice/status")


@router.post("/voice/compliance-status")
async def proxy_voice_compliance_status(request: Request) -> Response:
    return await _proxy_http(request, "/voice/compliance-status")


@router.post("/voice/transfer-xml")
async def proxy_voice_transfer_xml(request: Request) -> Response:
    # Query string (the destination number) travels with the request
    # automatically - Request.url.query on the voice service's own side
    # sees the same string a client sent to this proxy, since httpx's
    # target URL below carries it through unchanged.
    query = f"?{request.url.query}" if request.url.query else ""
    return await _proxy_http(request, f"/voice/transfer-xml{query}")


@router.websocket("/voice/stream")
async def proxy_voice_stream(websocket: WebSocket) -> None:
    """
    Verify, then relay frames both directions until either side closes -
    nothing here understands Plivo's or Sarvam's message shapes, on purpose;
    that logic lives once, in the real voice service.
    """
    try:
        # Signed as http://, not wss:// or https:// - confirmed empirically
        # against a real call (see relay.py's own verify() call, which this
        # mirrors exactly since both check the same signed URL).
        verify(
            uri=f"http://{settings.public_base_url.split('://', 1)[-1].rstrip('/')}/voice/stream",
            signature=websocket.headers.get("x-plivo-signature-ma-v3"),
            nonce=websocket.headers.get("x-plivo-signature-v3-nonce"),
            method="GET",
        )
    except InvalidSignature:
        logger.warning("rejected voice websocket upgrade at proxy - bad plivo signature")
        await websocket.close(code=4003)
        return

    await websocket.accept()

    # Forwarded unchanged: the voice service's own verify() checks these
    # same two header values against the same settings.public_base_url, and
    # does not care that the connection arrived from this proxy rather than
    # directly from Plivo.
    forwarded_headers = {
        k: v
        for k, v in websocket.headers.items()
        if k.lower()
        in ("x-plivo-signature-ma-v3", "x-plivo-signature-v3", "x-plivo-signature-v3-nonce")
    }

    try:
        upstream = await websockets.connect(
            f"{_VOICE_WS_BASE}/voice/stream", additional_headers=forwarded_headers, open_timeout=10
        )
    except Exception:
        logger.exception("could not reach voice service for /voice/stream")
        await websocket.close(code=1011)
        return

    async def pump_down() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(message)

    async def pump_up() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if "text" in message and message["text"] is not None:
                await upstream.send(message["text"])
            elif "bytes" in message and message["bytes"] is not None:
                await upstream.send(message["bytes"])

    down_task = asyncio.create_task(pump_down())
    up_task = asyncio.create_task(pump_up())
    try:
        done, pending = await asyncio.wait(
            {down_task, up_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception() if task.cancelled() is False else None
            if exc and not isinstance(exc, (WebSocketDisconnect, websockets.exceptions.ConnectionClosed)):
                logger.exception("voice stream proxy pump failed", exc_info=exc)
    finally:
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass


async def _relay_websocket(websocket: WebSocket, upstream_url: str) -> None:
    """
    The same bidirectional pump proxy_voice_stream uses above, factored out
    only for the two newer sockets below - proxy_voice_stream itself is
    left untouched rather than rewritten to call this, since it is already
    verified working against real calls and there is no reason to risk it
    for a refactor.
    """
    try:
        upstream = await websockets.connect(upstream_url, open_timeout=10)
    except Exception:
        logger.exception("could not reach voice service for %s", upstream_url)
        await websocket.close(code=1011)
        return

    async def pump_down() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(message)

    async def pump_up() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if "text" in message and message["text"] is not None:
                await upstream.send(message["text"])
            elif "bytes" in message and message["bytes"] is not None:
                await upstream.send(message["bytes"])

    down_task = asyncio.create_task(pump_down())
    up_task = asyncio.create_task(pump_up())
    try:
        done, pending = await asyncio.wait(
            {down_task, up_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception() if task.cancelled() is False else None
            if exc and not isinstance(exc, (WebSocketDisconnect, websockets.exceptions.ConnectionClosed)):
                logger.exception("voice proxy pump failed for %s", upstream_url, exc_info=exc)
    finally:
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/voice/copilot-stream")
async def proxy_voice_copilot_stream(websocket: WebSocket) -> None:
    """Plivo's listen-only audio fork for copilot-mode calls - same signature check as /voice/stream."""
    try:
        verify(
            uri=f"http://{settings.public_base_url.split('://', 1)[-1].rstrip('/')}"
            "/voice/copilot-stream",
            signature=websocket.headers.get("x-plivo-signature-ma-v3"),
            nonce=websocket.headers.get("x-plivo-signature-v3-nonce"),
            method="GET",
        )
    except InvalidSignature:
        logger.warning("rejected copilot voice websocket upgrade at proxy - bad plivo signature")
        await websocket.close(code=4003)
        return

    await websocket.accept()
    await _relay_websocket(websocket, f"{_VOICE_WS_BASE}/voice/copilot-stream")


@router.websocket("/voice/copilot-assist")
async def proxy_voice_copilot_assist(websocket: WebSocket, token: str) -> None:
    """
    A staff member's dashboard, not a telephony callback - no Plivo
    signature to check here. The voice service's own copilot_assist
    endpoint verifies the access token itself; this proxy only relays.
    """
    await websocket.accept()
    from urllib.parse import quote

    await _relay_websocket(
        websocket, f"{_VOICE_WS_BASE}/voice/copilot-assist?token={quote(token)}"
    )
