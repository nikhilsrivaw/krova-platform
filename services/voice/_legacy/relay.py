"""
The Conversation Relay WebSocket endpoint.

Telnyx handles speech-to-text and text-to-speech; this socket carries
text in both directions for the whole call. That means no audio buffers,
no codecs and no GPU here - which is why this is the right Phase 1.

Messages Telnyx sends us:
  {"type": "setup",  ...call metadata...}
  {"type": "prompt", "voicePrompt": "...", "lang": "en", "last": true}
  {"type": "interrupt", ...}

Messages we send back:
  {"type": "text", "token": "...", "last": false}
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app import metrics
from app.services import call_service
from app.session import CallSession
from app.tenants import resolve_tenant

logger = logging.getLogger(__name__)

router = APIRouter()

# Strong references to in-flight cleanup tasks, so they are not garbage
# collected while completing after their websocket scope has gone.
_cleanup_tasks: set[asyncio.Task] = set()


async def _finalise(session) -> None:
    """
    Close the call record without being cancelled half-way.

    When the caller hangs up, Starlette cancels this handler's task
    scope - which would abort the writes that record duration, cost and
    the last transcript line. Those are billing data, so the cleanup is
    run as a detached, shielded task that survives the cancellation.
    """
    task = asyncio.create_task(session.close())
    _cleanup_tasks.add(task)
    task.add_done_callback(_cleanup_tasks.discard)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # The scope died; the task keeps running to completion.
        pass


def _first(payload: dict, *keys: str) -> str | None:
    """
    Read the first key that is present.

    Telnyx sends camelCase in the relay socket but snake_case in REST
    webhooks, and field names have shifted between versions - so accept
    both rather than break on a rename.
    """
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    return None


@router.websocket("/relay")
async def relay(websocket: WebSocket) -> None:
    await websocket.accept()
    session: CallSession | None = None

    try:
        while True:
            message = await websocket.receive_json()
            msg_type = message.get("type")

            if msg_type == "setup":
                call_id = (
                    _first(message, "callControlId", "call_control_id", "callSid")
                    or "unknown"
                )
                to_number = _first(message, "to", "To")
                from_number = _first(message, "from", "From")

                config = await resolve_tenant(to_number)

                # Open the billing/transcript record. A logging failure
                # must never stop a live call, so this is best-effort.
                db_call_id = None
                try:
                    db_call_id = await call_service.start_call(
                        tenant_id=config.tenant_id,
                        provider_call_id=call_id,
                        from_number=from_number,
                        to_number=to_number,
                    )
                except Exception:
                    logger.warning("could not open call record", exc_info=True)

                session = CallSession(websocket, config, call_id, db_call_id)
                metrics.active_calls.inc()

                logger.info(
                    "relay setup call=%s tenant=%s from=%s to=%s",
                    call_id,
                    config.tenant_id,
                    from_number,
                    to_number,
                )
                await session.say(config.greeting)

            elif msg_type == "prompt":
                if session is None:
                    logger.warning("prompt before setup - ignoring")
                    continue
                # Only act on a finished utterance; partials would make
                # the agent talk over the caller mid-sentence.
                if not message.get("last", True):
                    continue
                text = _first(message, "voicePrompt", "voice_prompt", "text")
                if text:
                    await session.on_prompt(text)

            elif msg_type == "interrupt":
                if session:
                    await session.on_interrupt()

            elif msg_type in {"error", "end"}:
                logger.info("relay %s: %s", msg_type, message)
                break

            else:
                logger.debug("unhandled relay message: %s", msg_type)

    except WebSocketDisconnect:
        logger.info(
            "relay disconnected call=%s",
            session.provider_call_id if session else "-",
        )
    except Exception:
        logger.exception("relay error")
    finally:
        if session:
            metrics.active_calls.dec()
            metrics.calls_total.labels(session.config.tenant_id, "completed").inc()
            await _finalise(session)
