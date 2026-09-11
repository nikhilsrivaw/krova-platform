"""
Wiring three live sockets together: Plivo, Sarvam STT, Sarvam TTS.

This is the only module in the voice channel that opens real network
connections. Everything it does is delegate to pipeline.py, sarvam.py and
xml.py, which are unit-testable without a phone anywhere near them - this
file is the thin, largely untestable layer that connects them to the wire.

The upgrade itself is verified against Plivo's v3 signature before anything
else happens - the same check the WhatsApp webhook does against Meta, applied
here to a WebSocket instead of a POST. Skipping it would let anyone who finds
this URL open a live conversation with the agent, and on a business whose
autonomy is set to `act`, put words in its mouth in front of a real caller.

Three tasks run for the length of a call:

  1. read Plivo's audio frames, forward them to Sarvam STT
  2. read Sarvam's transcripts, feed them to the pipeline
  3. read whatever the pipeline decides to say, push it to Plivo as playAudio

All three must stop together when the call ends - a leaked STT or TTS
connection after Plivo hangs up is a socket nobody closes and a cost nobody
notices until the bill arrives.
"""

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.ai import call_summary
from shared.ai import context as agent_context
from shared.ai import outbound_opener
from shared.auth.encryption import decrypt
from shared.billing import usage
from shared.channels.voice import outbound, plivo_client, sarvam
from shared.channels.voice.call_registry import recall as recall_call
from shared.channels.voice.plivo_signature import InvalidSignature, verify
from shared.channels.voice.pipeline import CallPipeline
from shared.channels.voice.tenant import VoiceRoute, resolve
from shared.config.settings import settings
from shared.db.models import (
    Business,
    Call,
    CallScriptResponse,
    ChannelConnection,
    Customer,
    CustomerIdentity,
    Direction,
    IdentityKind,
    UsageEventType,
)
from shared.identity.normalise import InvalidIdentifier, normalise
from shared.db.session import AsyncSessionLocal
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()

# Detached cleanup tasks, kept referenced so they are not garbage-collected
# mid-flight when the websocket's own scope ends first.
_cleanup_tasks: set[asyncio.Task] = set()

# Pre-baked with scripts/generate_filler_audio.py, not synthesised live: the
# whole point is to have something to play with zero network round-trips
# while the real greeting's TTS handshake is still connecting. Confirmed on
# real calls that this handshake sometimes takes 3-5s+ against a baseline of
# well under 2s (see _connect_sarvam's own retry-at-longer-timeout, added for
# the same reason) - long enough that a caller hears pure silence and hangs
# up before ever hearing the greeting.
_FILLER_AUDIO_PATH = Path(__file__).resolve().parent / "assets" / "filler_en.ulaw"
_FILLER_AUDIO: bytes = (
    _FILLER_AUDIO_PATH.read_bytes() if _FILLER_AUDIO_PATH.exists() else b""
)
if not _FILLER_AUDIO:
    logger.warning(
        "no filler audio at %s - run scripts/generate_filler_audio.py; "
        "calls will get silence instead of filler during a slow TTS connect",
        _FILLER_AUDIO_PATH,
    )

# Below the real baseline (0.92-1.67s measured on real calls) so it almost
# never fires on a healthy connect, but well above it so a normal connect
# finishing a little late still beats the filler to the caller's ear.
_FILLER_GUARD_DELAY_SECONDS = 2.0


async def _connect_sarvam(url: str):
    """
    Open a Sarvam websocket, tolerating the handshake occasionally taking
    longer than the 10s default - confirmed against real calls, where a
    connection that succeeds in well under a second standalone would time
    out here often enough to matter. One retry at a longer timeout rather
    than raising the default everywhere, since most connections do not
    need it.
    """
    headers = sarvam.auth_headers()
    try:
        return await websockets.connect(url, additional_headers=headers, open_timeout=15)
    except TimeoutError:
        logger.warning("sarvam handshake slow, retrying once: %s", url)
        return await websockets.connect(url, additional_headers=headers, open_timeout=20)


async def connect_with_filler(
    connect,
    *,
    send_chunk,
    audio: bytes,
    sample_rate: int,
    guard_delay: float,
    call_uuid: str,
):
    """
    Race a TTS connect against a looping filler clip, so a caller never sits
    in silence while it is slow - and stays correctly scoped to *this one*
    connect, not the whole call.

    An earlier version ran one filler loop for the whole call, gated on
    "has any audio ever been sent yet", started once next to the greeting.
    Confirmed broken on a real call: the caller spoke *during* the filler
    (which is exactly what a "one moment, connecting you" phrase invites),
    that correctly triggered a real barge-in - cancelling the greeting and
    clearing Plivo's queue - but the filler task had no idea any of that
    happened, kept looping on the same stale signal, and queued another
    full clip on top of the caller's own interruption. This version has no
    call-wide state at all: it takes the connect attempt itself as an
    argument, races it, and returns/raises whatever that connect does.
    Called once per turn (greeting or any reply) from inside `speak()`, so
    when a barge-in cancels that turn's task the cancellation reaches this
    coroutine and its own child connect task the same way it already
    reaches an in-progress TTS stream - no separate task to leak or forget
    to cancel.

    Whole clip per send, not sliced into small pieces on a timer: a still
    earlier version chopped it into 100ms pieces and slept 100ms between
    each, confirmed on a real call to make the filler itself sound broken
    up - network/processing time on top of that sleep supplies audio to
    Plivo slower than it plays it. Sending the whole clip at once puts it
    all in Plivo's buffer ahead of playback, so there is nothing to starve.

    Free-standing rather than inline in `speak()` so it is testable against
    a fake clock and a fake connect/sender, the same reason `sarvam.py`'s
    clients take an injected websocket rather than opening one internally.
    """
    connect_task = asyncio.ensure_future(connect())
    try:
        try:
            return await asyncio.wait_for(asyncio.shield(connect_task), timeout=guard_delay)
        except TimeoutError:
            pass

        if audio:
            logger.info("tts connect slow, playing filler call=%s", call_uuid)
            clip_seconds = len(audio) / sample_rate
            while not connect_task.done():
                await send_chunk(audio)
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(connect_task), timeout=clip_seconds
                    )
                except TimeoutError:
                    continue
        return await connect_task
    except asyncio.CancelledError:
        if not connect_task.done():
            connect_task.cancel()
        raise


# Plivo's real answer, not a guess: this is an account-wide limit shared by
# every business's subaccount, not something Krova can raise per-tenant. A
# capacity request to Plivo is the only real fix once this is regularly hit.
_PLIVO_CONCURRENT_CALL_LIMIT = 25
_CAPACITY_WARN_THRESHOLD = 20


async def _warn_if_near_capacity(db: AsyncSession) -> None:
    """
    Log loudly when the platform - all businesses combined - is close to
    Plivo's account-wide concurrent-call ceiling.

    Not a hard block: this counts unclosed rows in our own database, which
    can drift from Plivo's real live-call count if a row fails to close
    cleanly, so refusing calls on it risks rejecting real ones on bad data.
    Visibility now is worth more than an unreliable block.
    """
    count = await db.scalar(select(func.count()).select_from(Call).where(Call.ended_at.is_(None)))
    if count is not None and count >= _CAPACITY_WARN_THRESHOLD:
        logger.warning(
            "voice concurrency at %s/%s of Plivo's account-wide limit - "
            "request higher capacity from Plivo before this is hit",
            count,
            _PLIVO_CONCURRENT_CALL_LIMIT,
        )


def _first(payload: dict, *keys: str) -> str | None:
    """Plivo's field names have shifted between API versions - accept several."""
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    return None


@router.websocket("/voice/stream")
async def stream(
    websocket: WebSocket,
    recipient_id: str | None = None,
    business_id: str | None = None,
    customer_id: str | None = None,
    reason: str | None = None,
) -> None:
    """
    The socket Plivo's <Stream> element connects to for one call.

    `recipient_id` is present only for an outbound campaign call (see
    outbound.py's outbound_answer, which builds the wss:// URL with it) -
    None (the default) is every existing inbound call, unchanged.
    `business_id`+`customer_id` together mark an adhoc call instead (see
    outbound.py's adhoc_answer) - a proactive call with no CallCampaign
    behind it, same outbound shape otherwise.
    """
    try:
        # Plivo signs the stream URL with a bare http:// scheme regardless of
        # the wss:// URL actually handed to it in the <Stream> element or the
        # https the request arrives over. AND - confirmed for real this
        # session, by brute-forcing candidate signed strings against a
        # genuine (nonce, received-signature) pair captured from production
        # logs until one actually reproduced Plivo's own signature - the
        # query string is not part of what gets signed for a stream
        # WebSocket upgrade at all, unlike a regular GET/POST webhook.
        # business_id/customer_id/reason (or recipient_id, for an outbound
        # campaign call) still travel in the query for routing; they are
        # just outside the signed value. A same-session first attempt at
        # this fix (matching the literal wss:// scheme, keeping the query)
        # was itself wrong - both changed at once made it impossible to
        # isolate which one mattered, which the brute-force search resolved
        # directly against real data instead of guessing again.
        #
        # Verified against Ma-V3 (parent token), not V3 (subaccount token):
        # this one endpoint serves every business's calls, and at the moment
        # of accepting the upgrade there is no call data yet to say whose
        # subaccount it belongs to - Ma-V3 needs no such lookup.
        verify(
            uri=f"http://{settings.public_base_url.split('://', 1)[-1].rstrip('/')}"
            "/voice/stream",
            signature=websocket.headers.get("x-plivo-signature-ma-v3"),
            nonce=websocket.headers.get("x-plivo-signature-v3-nonce"),
            method="GET",
        )
    except InvalidSignature:
        logger.warning("rejected voice websocket upgrade - bad plivo signature")
        await websocket.close(code=4003)
        return

    await websocket.accept()

    stt_ws = None
    tts_ws = None
    call_row_id: uuid.UUID | None = None
    route: VoiceRoute | None = None
    pipeline: CallPipeline | None = None
    stream_id: str | None = None
    call_start = time.time()

    # A filler clip (sent from inside speak(), see below) and real audio
    # are never concurrent writers by construction now - connect_with_filler
    # is awaited sequentially before speak() ever starts forwarding real
    # chunks - but the lock stays cheap insurance against any future path
    # that writes to this socket from two tasks at once.
    _ws_send_lock = asyncio.Lock()

    async def _send_wire_audio(mulaw_bytes: bytes) -> None:
        async with _ws_send_lock:
            await websocket.send_text(
                json.dumps(
                    {
                        "event": "playAudio",
                        "media": {
                            "contentType": "audio/x-mulaw",
                            "sampleRate": 8000,
                            "payload": _b64(mulaw_bytes),
                        },
                    }
                )
            )

    async def send_audio(mulaw_bytes: bytes) -> None:
        await _send_wire_audio(mulaw_bytes)

    async def send_clear() -> None:
        if stream_id:
            async with _ws_send_lock:
                await websocket.send_text(
                    json.dumps({"event": "clearAudio", "streamId": stream_id})
                )

    _prewarmed_tts: asyncio.Task | None = None

    def prewarm_tts() -> None:
        """
        Start opening the next TTS connection before there is any text to
        send it - called the moment a caller's utterance finalises, so the
        ~1-1.3s handshake runs concurrently with the ~2-3.5s Claude call
        that decides what to say, instead of stacking after it. Measured
        this as the single largest remaining lever once Claude's own
        latency turned out close to its floor for the context size a real
        reply needs.
        """
        nonlocal _prewarmed_tts
        if _prewarmed_tts is None or _prewarmed_tts.done():
            _prewarmed_tts = asyncio.create_task(_connect_sarvam(sarvam.tts_connect_url()))

    async def speak(text_chunks):
        """
        Speak a reply as its text arrives, not after all of it exists.

        One TTS connection for the whole reply, config sent once - not one
        connection per sentence. Each text chunk gets its own flush, so
        Sarvam starts synthesising it the instant it lands rather than
        waiting to accumulate a bigger buffer: audio for the first sentence
        can already be reaching the caller while a longer reply's later
        sentences are still being generated upstream by Claude. For a
        single-chunk input (the greeting, always one fixed string) this
        degrades to exactly the old one-shot behaviour.

        Reading and writing happen concurrently, in two tasks sharing one
        queue, because they cannot be sequential: waiting for chunk 1's
        full audio before sending chunk 2's text would serialise exactly
        what this exists to overlap. `finals_received >= flushes_sent` is
        how the reader knows there is truly nothing left to arrive - proven
        live to fire once per flush, in order, even when several flushes
        are sent back to back with no wait between them.
        """
        nonlocal _prewarmed_tts
        ws = None
        if _prewarmed_tts is not None:
            pending, _prewarmed_tts = _prewarmed_tts, None
            try:
                ws = await pending
            except Exception:
                logger.warning("prewarmed tts connection failed, connecting fresh")
                ws = None
        if ws is None:
            ws = await connect_with_filler(
                lambda: _connect_sarvam(sarvam.tts_connect_url()),
                send_chunk=_send_wire_audio,
                audio=_FILLER_AUDIO,
                sample_rate=8000,
                guard_delay=_FILLER_GUARD_DELAY_SECONDS,
                call_uuid=call_uuid,
            )

        # What the caller actually spoke, if Sarvam's STT has reported one
        # yet, over the connection's static configured language - a caller
        # code-mixing Hindi and English gets a reply spoken the same way,
        # not forced into whichever language_code the business happened to
        # be set up with. A business can override this adaptive behaviour by
        # setting language_mode to "fixed" - e.g. a Hindi-only agent that
        # always replies in Hindi regardless of what a caller mixes in.
        if route and route.language_mode == "fixed":
            language = route.language
        else:
            language = (pipeline.detected_language if pipeline else None) or (
                route.language if route else "en-IN"
            )
        speaker = route.speaker if route else "shubh"

        async with ws:
            await ws.send(sarvam.tts_start_config(language=language, speaker=speaker))

            audio_q: asyncio.Queue = asyncio.Queue()
            state = {"flushes_sent": 0, "finals_received": 0, "sender_done": False}

            async def reader():
                try:
                    async for raw in ws:
                        if sarvam.is_tts_final_event(raw):
                            state["finals_received"] += 1
                            if state["sender_done"] and state["finals_received"] >= state["flushes_sent"]:
                                await audio_q.put(None)
                                return
                            continue
                        chunk = sarvam.parse_tts_event(raw)
                        if chunk is not None:
                            await audio_q.put(chunk)
                except websockets.exceptions.ConnectionClosed:
                    pass
                except Exception:
                    logger.exception("tts stream reader failed")
                finally:
                    await audio_q.put(None)

            reader_task = asyncio.create_task(reader())

            try:
                async for text in text_chunks:
                    await ws.send(sarvam.tts_text_frame(text))
                    await ws.send(sarvam.tts_flush_frame())
                    state["flushes_sent"] += 1
            finally:
                state["sender_done"] = True
                if state["finals_received"] >= state["flushes_sent"]:
                    await audio_q.put(None)

            try:
                while True:
                    item = await audio_q.get()
                    if item is None:
                        break
                    yield item
            finally:
                if not reader_task.done():
                    reader_task.cancel()
                    try:
                        await reader_task
                    except (asyncio.CancelledError, Exception):
                        pass

    try:
        while True:
            raw = await websocket.receive_text()
            message = json.loads(raw)
            event = message.get("event")

            if event == "start":
                start_info = message.get("start", {})
                stream_id = start_info.get("streamId")
                call_uuid = (
                    _first(start_info, "callId", "callUUID", "CallUUID")
                    or stream_id
                    or "unknown"
                )

                # Plivo's start event carries no To/From - confirmed against a
                # real call, only callId/streamId/accountId/tracks/mediaFormat.
                # Those numbers were captured moments earlier in /voice/answer,
                # under the same CallUUID.
                remembered = recall_call(call_uuid) or {}
                to_number = remembered.get("to") or _first(message, "to", "To")
                from_number = remembered.get("from") or _first(message, "from", "From")
                # Only ever set for a call whose /voice/answer webhook was
                # captured under this same call_uuid - see call_registry.py's
                # own docstring on why this is the one place it can be
                # captured at all.
                ring_started_at = remembered.get("ring_started_at")

                async with AsyncSessionLocal() as db:
                    opening_line: str | None = None
                    opening_stream = None
                    opener_cost: dict = {"paise": 0}
                    outbound_customer_id: uuid.UUID | None = None
                    direction = Direction.inbound
                    pipeline_mode = "customer"
                    scripted_context = None
                    call_script_id: uuid.UUID | None = None

                    if recipient_id:
                        # Outbound campaign call - already knows business +
                        # customer from the CallCampaignRecipient, so it
                        # skips resolve()'s phone-number lookup entirely
                        # (meaningless here: the "to" Plivo reports on an
                        # outbound leg is the customer, not one of KROVA's
                        # own numbers).
                        outbound_context = await outbound.build_context(
                            uuid.UUID(recipient_id), db
                        )
                        if outbound_context is None:
                            logger.warning(
                                "outbound stream recipient=%s could not build a call "
                                "context - hanging up",
                                recipient_id,
                            )
                            await websocket.close()
                            return
                        route = outbound_context.route
                        from_number = outbound_context.customer_phone
                        opening_line = outbound_context.opening_line
                        outbound_customer_id = outbound_context.customer_id
                        direction = Direction.outbound
                        if outbound_context.call_script_id is not None:
                            pipeline_mode = "scripted"
                            scripted_context = outbound_context.scripted_context
                            call_script_id = outbound_context.call_script_id
                    elif business_id and customer_id:
                        # Adhoc proactive call (shared/care/
                        # commitment_deadline_calls.py today) - same shape
                        # as the campaign branch above, minus the
                        # CallCampaignRecipient lookup.
                        # skip_opener: the line itself is streamed below
                        # rather than drafted-then-handed-over, so this only
                        # needs the route/phone/customer half.
                        outbound_context = await outbound.build_adhoc_context(
                            uuid.UUID(business_id), uuid.UUID(customer_id), reason or "", db,
                            skip_opener=True,
                        )
                        if outbound_context is None:
                            logger.warning(
                                "adhoc stream business=%s customer=%s could not build a "
                                "call context - hanging up",
                                business_id, customer_id,
                            )
                            await websocket.close()
                            return
                        route = outbound_context.route
                        from_number = outbound_context.customer_phone
                        outbound_customer_id = outbound_context.customer_id
                        direction = Direction.outbound
                        adhoc_reason = reason or ""
                        adhoc_business_id = uuid.UUID(business_id)
                        adhoc_customer_id = uuid.UUID(customer_id)

                        # Already written while the phone was ringing (see
                        # outbound.place_adhoc_call) - speak it, don't
                        # generate a second one. Only when it is missing -
                        # an older call already in flight, or a drafting
                        # failure at dial time - does this fall back to
                        # generating live below.
                        predrafted = (remembered or {}).get("opening_line")
                        if predrafted:
                            opening_line = predrafted
                            logger.info(
                                "using pre-drafted opening line call=%s", call_uuid,
                            )

                        def _adhoc_opening():
                            """
                            Built here rather than inside build_adhoc_context
                            so the caller hears sentence one while Claude is
                            still writing sentence two - the whole reason
                            draft_stream exists. Cost lands in the ledger
                            below, once the stream has been consumed.
                            """
                            async def _gen():
                                context = await agent_context.build(
                                    adhoc_business_id, adhoc_customer_id, db
                                )
                                async for piece in outbound_opener.draft_stream(
                                    context, reason=adhoc_reason, cost_sink=opener_cost,
                                ):
                                    yield piece

                            return _gen()

                        # Only when nothing was pre-drafted: pipeline.start
                        # prefers opening_stream over opening_line, so
                        # setting both would throw the ready line away.
                        if not predrafted:
                            opening_stream = _adhoc_opening
                    else:
                        route = await resolve(to_number or "", db)
                        if route is None:
                            logger.warning("call to unrecognised number %s - hanging up", to_number)
                            await websocket.close()
                            return

                    # Own-number check applies to every branch above too
                    # (an outbound/adhoc call is never the owner calling
                    # in), but only inbound calls can plausibly match it -
                    # route is guaranteed set past this point either way.
                    # Never overwrites an already-scripted mode from the
                    # recipient_id branch above.
                    if pipeline_mode == "customer" and from_number and route.owner_phone:
                        try:
                            if normalise(IdentityKind.phone.value, from_number) == route.owner_phone:
                                pipeline_mode = "owner"
                        except InvalidIdentifier:
                            pass

                    call_row = Call(
                        business_id=route.business_id,
                        connection_id=route.connection_id,
                        direction=direction,
                        external_id=call_uuid,
                        transport="plivo",
                        ring_started_at=ring_started_at,
                        started_at=datetime.now(timezone.utc),
                        answered_at=datetime.now(timezone.utc),
                    )
                    db.add(call_row)
                    await db.flush()
                    call_row_id = call_row.id
                    await db.commit()

                    await _warn_if_near_capacity(db)

                    # A repeat caller Sarvam already correctly identified
                    # last call gets that language back for the greeting -
                    # the one moment real-time detection can't cover, since
                    # nothing has been heard yet. Read-only lookup (never
                    # resolve()'s find-or-create - a call that never speaks
                    # a word must not create a Customer row), so a caller
                    # with no history simply leaves this None, today's
                    # exact behavior.
                    known_language: str | None = None
                    if pipeline_mode == "owner":
                        # No Customer row is ever looked up for an owner
                        # call - see CallPipeline.mode's own docstring.
                        pass
                    elif outbound_customer_id is not None:
                        known_customer = await db.get(Customer, outbound_customer_id)
                        known_language = known_customer.preferred_language if known_customer else None
                    elif from_number:
                        try:
                            normalised_phone = normalise(IdentityKind.phone.value, from_number)
                        except InvalidIdentifier:
                            normalised_phone = None
                        if normalised_phone:
                            existing_customer_id = (
                                await db.execute(
                                    select(CustomerIdentity.customer_id).where(
                                        CustomerIdentity.business_id == route.business_id,
                                        CustomerIdentity.kind == IdentityKind.phone,
                                        CustomerIdentity.value == normalised_phone,
                                    )
                                )
                            ).scalar_one_or_none()
                            if existing_customer_id is not None:
                                known_customer = await db.get(Customer, existing_customer_id)
                                known_language = known_customer.preferred_language if known_customer else None

                    pipeline = CallPipeline(
                        route=route,
                        caller_phone=from_number or "unknown",
                        provider_call_id=call_uuid,
                        send_audio=send_audio,
                        send_clear=send_clear,
                        speak=speak,
                        db=db,
                        call_row_id=call_row_id,
                        opening_line=opening_line,
                        opening_stream=opening_stream,
                        customer_id=outbound_customer_id,
                        detected_language=known_language,
                        mode=pipeline_mode,
                        scripted_context=scripted_context,
                        call_script_id=call_script_id,
                    )

                    logger.info(
                        "call started business=%s from=%s call=%s",
                        route.business_id,
                        from_number,
                        call_uuid,
                    )
                    # Backgrounded, not awaited: this coroutine must return to
                    # receive_text() immediately so caller audio keeps
                    # flowing to STT while the greeting plays - awaiting it
                    # inline blocked the whole receive loop for the length of
                    # the greeting, confirmed on a real call where Sarvam
                    # logged zero audio received for the entire call.
                    #
                    # Started before STT connects, not after: the greeting
                    # does not depend on STT at all, and a real call showed
                    # a 5-6s delay before the greeting's own TTS connection
                    # even began - the STT handshake ahead of it was simply
                    # slow that time, and nothing needed it to finish first.
                    #
                    # Tracked as _reply_task, not fire-and-forget: without
                    # this, a caller talking over the greeting produced two
                    # _say() calls sending playAudio concurrently - the
                    # greeting's and the reply's chunks interleaved on the
                    # same stream, which is what "not clearly audible" on a
                    # real call traced back to. Barge-in's existing cancel
                    # logic works unchanged once the greeting is just another
                    # tracked turn.
                    # Open the TTS socket now, not when speak() first needs
                    # it. Until this, prewarm_tts only ever fired on a
                    # finalised transcript - i.e. from the caller's SECOND
                    # turn onwards - so the one turn that most needs it, the
                    # very first thing anyone hears, was the only turn that
                    # always paid the full handshake (0.92-1.67s measured,
                    # occasionally 3-5s) in series. Started before
                    # pipeline.start() so the handshake overlaps Claude
                    # drafting the opening line instead of following it.
                    prewarm_tts()

                    pipeline._reply_task = asyncio.create_task(pipeline.start())

                    if opening_stream is not None and call_row_id is not None:
                        # The streamed opener prices itself only once fully
                        # consumed (see draft_stream's cost_sink), so this
                        # waits on the same task rather than reading a value
                        # that is still 0 - detached, so nothing here delays
                        # the STT connect below.
                        cost_task = asyncio.create_task(
                            _record_opener_cost(
                                pipeline._reply_task, opener_cost, route.business_id, call_row_id,
                            )
                        )
                        _cleanup_tasks.add(cost_task)
                        cost_task.add_done_callback(_cleanup_tasks.discard)

                    stt_ws = await _connect_sarvam(sarvam.stt_connect_url(language="auto"))
                    asyncio.create_task(_pump_transcripts(stt_ws, pipeline, prewarm_tts))

            elif event == "media":
                if stt_ws is None:
                    continue
                payload = (message.get("media") or {}).get("payload")
                if payload:
                    await stt_ws.send(
                        sarvam.stt_audio_frame(_b64decode(payload))
                    )

            elif event == "dtmf":
                # Plivo's RFC-2833 keypress event, delivered over this same
                # socket independent of the audio codec - confirmed against
                # Plivo's own streaming docs. Only "0" (the universal "reach
                # a person" convention) is handled; every other digit is a
                # no-op for now, no full keypad menu.
                digit = (message.get("dtmf") or {}).get("digit")
                if digit == "0" and pipeline is not None:
                    pipeline._reply_task = asyncio.create_task(pipeline.request_transfer())

            elif event in ("stop", "end"):
                logger.info("call ended stream=%s", stream_id)
                break

            else:
                logger.debug("unhandled plivo event: %r", event)

    except WebSocketDisconnect:
        logger.info("caller disconnected stream=%s", stream_id)
    except Exception:
        logger.exception("voice relay error")
    finally:
        # Otherwise a caller hanging up mid-reply left the in-flight Claude
        # call and TTS stream running to completion anyway - cost for a
        # reply nobody would ever hear, and a `send_audio` at the end
        # writing to a websocket that had already closed.
        if pipeline is not None and pipeline._reply_task is not None:
            pipeline._reply_task.cancel()
        if stt_ws is not None:
            await _safe_close(stt_ws)
        if _prewarmed_tts is not None and not _prewarmed_tts.done():
            _prewarmed_tts.cancel()
        agent_chars = sum(len(t.text) for t in pipeline.turns if t.role == "agent") if pipeline else 0
        task = asyncio.create_task(
            _finalise_call(
                call_row_id, call_start, agent_chars,
                customer_id=pipeline.customer_id if pipeline else None,
                detected_language=pipeline.detected_language if pipeline else None,
            )
        )
        _cleanup_tasks.add(task)
        task.add_done_callback(_cleanup_tasks.discard)

        # Snapshotted into plain dicts now, not read lazily from pipeline.turns
        # inside the detached task - pipeline is a local variable kept alive by
        # the closure either way, but the transcript itself needs no more than
        # its role/text, so there is nothing to gain from holding the whole
        # object alive longer than this function's own scope.
        transcript = (
            [{"role": t.role, "text": t.text} for t in pipeline.turns if t.text.strip()]
            if pipeline
            else []
        )
        # A fully separate detached task from _finalise_call, not folded into
        # it: that one records billing data, already proven correct - a bug
        # in this new analytics path must never be able to risk it.
        analytics_task = asyncio.create_task(
            _analyze_call(
                call_row_id,
                route.business_id if route else None,
                transcript,
                pipeline.call_script_id if pipeline else None,
            )
        )
        _cleanup_tasks.add(analytics_task)
        analytics_task.add_done_callback(_cleanup_tasks.discard)


async def _record_opener_cost(
    opening_task: asyncio.Task, cost: dict, business_id: uuid.UUID, call_row_id: uuid.UUID,
) -> None:
    """
    Price a streamed outbound opener once it has actually finished.

    Its own generation cost is only known after the stream is consumed, and
    the stream is consumed by the greeting task - so this waits on that
    task rather than reading a number that is still zero. Cancelled (the
    caller barged in, or hung up mid-opener) is a normal outcome, not an
    error: whatever was generated up to that point is still billable and
    still recorded.
    """
    try:
        await opening_task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("opening line task failed before its cost could be recorded")

    paise = int(cost.get("paise") or 0)
    if not paise:
        return
    try:
        async with AsyncSessionLocal() as db:
            usage.record(
                business_id=business_id,
                event_type=UsageEventType.ai_reply_generated,
                channel="voice",
                quantity=1,
                unit="call",
                krova_cost_paise=paise,
                source_type="call",
                source_id=call_row_id,
                db=db,
            )
            await db.commit()
    except Exception:
        logger.exception("could not record outbound opener cost call=%s", call_row_id)


async def _pump_transcripts(stt_ws, pipeline: CallPipeline, prewarm_tts) -> None:
    """Forward Sarvam transcripts into the pipeline until the socket closes."""
    try:
        async for transcript in sarvam.stream_transcripts(stt_ws):
            if transcript.is_final:
                # Fired before on_transcript, not after: the TTS handshake
                # needs every millisecond of head start it can get against
                # the Claude call on_transcript is about to kick off.
                prewarm_tts()
            await pipeline.on_transcript(
                transcript.text, is_final=transcript.is_final, language=transcript.language
            )
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception:
        logger.exception("transcript pump failed")


# Sarvam's confirmed rates (docs.sarvam.ai/api/getting-started/pricing,
# 24 Aug 2026): STT ₹30/hour billed per second; TTS bulbul:v2 ₹15/10,000
# characters.
_SARVAM_STT_RUPEES_PER_SECOND = 30 / 3600
_SARVAM_TTS_RUPEES_PER_CHAR = 15 / 10_000

# Plivo's CDR/Pricing amounts are in US dollars - confirmed against a real
# CDR and the account's own Pricing endpoint (a domestic India
# voice_network_group rate of "0.00475" is only plausible as USD/minute; as
# INR it would be under a paisa per minute). The earlier version of this
# file treated the same figure as rupees, undercounting Plivo's real cost by
# roughly this factor for every call before it was caught. No live forex
# feed is wired in; update this constant when it drifts meaningfully rather
# than trusting it silently forever.
_USD_TO_INR = 87.0


def _estimate_plivo_paise(duration_seconds: int, rate_per_min_usd: float | None) -> int:
    """Fallback only - used when the real CDR could not be fetched."""
    if not rate_per_min_usd:
        return 0
    return round((duration_seconds / 60) * rate_per_min_usd * _USD_TO_INR * 100)


async def _fetch_plivo_cdr(connection: ChannelConnection | None, call_uuid: str) -> dict | None:
    """
    The subaccount that owns the number if this connection has one, otherwise
    Krova's parent account - whichever actually placed the call. A call
    that just hung up may not have a CDR yet, so this retries briefly rather
    than falling straight back to the estimate on the first miss.
    """
    auth_id = settings.plivo_auth_id
    auth_token = settings.plivo_auth_token
    if connection is not None:
        sub_auth_id = (connection.extra or {}).get("subaccount_auth_id")
        if sub_auth_id and connection.access_token:
            auth_id = sub_auth_id
            auth_token = decrypt(connection.access_token)

    if not auth_id or not auth_token:
        return None

    for attempt in range(3):
        if attempt:
            await asyncio.sleep(2)
        try:
            cdr = await plivo_client.get_call_cdr(
                auth_id=auth_id, auth_token=auth_token, call_uuid=call_uuid
            )
        except plivo_client.PlivoError:
            logger.exception("plivo CDR fetch errored for call %s", call_uuid)
            return None
        if cdr is not None:
            return cdr
    return None


async def _finalise_call(
    call_row_id: uuid.UUID | None, started_at: float, agent_chars: int,
    *, customer_id: uuid.UUID | None = None, detected_language: str | None = None,
) -> None:
    """
    Close the call record after the socket handler's scope may already be gone.

    Runs detached so a caller hanging up does not race the write that records
    how long the call lasted - that duration is billing data.
    """
    if call_row_id is None:
        return
    try:
        async with AsyncSessionLocal() as db:
            call_row = await db.get(Call, call_row_id)
            if call_row is None:
                await db.commit()
                return

            call_row.ended_at = datetime.now(timezone.utc)
            call_row.duration_seconds = int(time.time() - started_at)

            # The call's own language preference improves every call it
            # is heard on, rather than being fixed once - only written
            # when Sarvam actually detected something and it differs from
            # what's already stored, so a call with no caller speech (e.g.
            # an immediate hangup) leaves the existing value untouched.
            if customer_id is not None and detected_language:
                customer = await db.get(Customer, customer_id)
                if customer is not None and customer.preferred_language != detected_language:
                    customer.preferred_language = detected_language

            connection = (
                await db.get(ChannelConnection, call_row.connection_id)
                if call_row.connection_id is not None
                else None
            )

            cdr = await _fetch_plivo_cdr(connection, call_row.external_id)
            if cdr is not None:
                plivo_paise = round(float(cdr.get("total_amount", 0)) * _USD_TO_INR * 100)
                plivo_source = "cdr"
                billed_seconds = cdr.get("billed_duration")
            else:
                raw_rate = (connection.extra or {}).get("voice_rate") if connection else None
                plivo_paise = _estimate_plivo_paise(
                    call_row.duration_seconds, float(raw_rate) if raw_rate else None
                )
                plivo_source = "estimate"
                billed_seconds = None

            stt_paise = round(call_row.duration_seconds * _SARVAM_STT_RUPEES_PER_SECOND * 100)
            tts_paise = round(agent_chars * _SARVAM_TTS_RUPEES_PER_CHAR * 100)

            call_row.cost_breakdown = {
                "sarvam_stt_paise": stt_paise,
                "sarvam_tts_paise": tts_paise,
                "plivo_voice_paise": plivo_paise,
                "plivo_cost_source": plivo_source,
            }
            call_row.cost_paise = stt_paise + tts_paise + plivo_paise

            usage.record(
                business_id=call_row.business_id,
                event_type=UsageEventType.voice_stt_seconds,
                channel="voice",
                quantity=call_row.duration_seconds,
                unit="second",
                krova_cost_paise=stt_paise,
                source_type="call",
                source_id=call_row.id,
                db=db,
            )
            usage.record(
                business_id=call_row.business_id,
                event_type=UsageEventType.voice_tts_characters,
                channel="voice",
                quantity=agent_chars,
                unit="character",
                krova_cost_paise=tts_paise,
                source_type="call",
                source_id=call_row.id,
                db=db,
            )
            usage.record(
                business_id=call_row.business_id,
                event_type=UsageEventType.voice_call_minutes,
                channel="voice",
                quantity=(billed_seconds if billed_seconds is not None else call_row.duration_seconds) / 60,
                unit="minute",
                krova_cost_paise=plivo_paise,
                source_type="call",
                source_id=call_row.id,
                extra={"cost_source": plivo_source},
                db=db,
            )

            await db.commit()
    except Exception:
        logger.exception("failed to close call record %s", call_row_id)


async def _analyze_call(
    call_row_id: uuid.UUID | None,
    business_id: uuid.UUID | None,
    transcript: list[dict],
    call_script_id: uuid.UUID | None = None,
) -> None:
    """
    Write a structured read on how the call went - outcome, sentiment,
    topic, a one-line summary - onto the Call row itself.

    Deliberately not shared/ai/signals.py: that module looks for
    noteworthy signals (a bug, a churn risk) and is gated to businesses
    whose vertical declares product_feedback (today, only "startup") -
    the wrong shape and the wrong scope for "how did this call go",
    which applies to every call on every vertical, not just ones that
    happen to contain a signal.

    Runs detached, same reasoning as _finalise_call: a caller hanging up
    must not block or be blocked by this.
    """
    if call_row_id is None or not transcript:
        return
    try:
        business_name = None
        async with AsyncSessionLocal() as db:
            if business_id is not None:
                business = await db.get(Business, business_id)
                business_name = business.name if business is not None else None

            result = await call_summary.summarize(
                transcript, business_name=business_name
            )
            if result is None:
                return

            call_row = await db.get(Call, call_row_id)
            if call_row is None:
                return

            call_row.outcome = result.outcome
            call_row.sentiment = result.sentiment
            call_row.topic = result.topic
            call_row.summary = result.summary

            if business_id is not None:
                from shared.db.models import WebhookEventType
                from shared.integrations import webhooks
                from shared.care import post_call_actions

                try:
                    await webhooks.dispatch_event(
                        db, business_id=business_id, event_type=WebhookEventType.call_completed.value,
                        payload={
                            "call_id": str(call_row_id),
                            "outcome": result.outcome,
                            "customer_id": str(call_row.customer_id) if call_row.customer_id else None,
                        },
                    )
                except Exception:
                    logger.exception("call.completed webhook dispatch failed call=%s", call_row_id)
                try:
                    await post_call_actions.apply_rules(
                        db, business_id=business_id, trigger_type=WebhookEventType.call_completed.value,
                        customer_id=call_row.customer_id, call_id=call_row_id,
                    )
                except Exception:
                    logger.exception("post-call action rules failed call=%s", call_row_id)

            if business_id is not None:
                usage.record(
                    business_id=business_id,
                    event_type=UsageEventType.ai_call_analysis,
                    channel="voice",
                    quantity=1,
                    unit="call",
                    krova_cost_paise=result.cost_paise,
                    source_type="call",
                    source_id=call_row_id,
                    db=db,
                )

            if call_script_id is not None:
                from shared.db.models import CallScript
                from shared.ai import call_script_extract

                script = await db.get(CallScript, call_script_id)
                if script is not None and script.questions:
                    extracted = await call_script_extract.extract(
                        transcript, questions=list(script.questions), purpose=script.purpose,
                        business_name=business_name,
                    )
                    if extracted is not None:
                        db.add(CallScriptResponse(
                            business_id=script.business_id,
                            call_script_id=script.id,
                            customer_id=call_row.customer_id,
                            call_id=call_row_id,
                            answers=extracted.answers,
                            score=extracted.score,
                            summary=extracted.summary,
                            created_at=datetime.now(timezone.utc),
                        ))
                        if business_id is not None and extracted.cost_paise:
                            usage.record(
                                business_id=business_id,
                                event_type=UsageEventType.ai_call_analysis,
                                channel="voice",
                                quantity=1,
                                unit="call",
                                krova_cost_paise=extracted.cost_paise,
                                source_type="call",
                                source_id=call_row_id,
                                db=db,
                            )
                        logger.info(
                            "call script extracted call=%s script=%s answers=%d score=%s",
                            call_row_id, call_script_id, len(extracted.answers), extracted.score,
                        )

            await db.commit()
    except Exception:
        logger.exception("failed to analyze call %s", call_row_id)


async def _safe_close(ws) -> None:
    try:
        await ws.close()
    except Exception:
        pass


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode()


def _b64decode(data: str) -> bytes:
    import base64

    return base64.b64decode(data)
