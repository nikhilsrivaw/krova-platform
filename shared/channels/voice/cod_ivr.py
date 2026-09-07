"""
The COD confirmation call's own answer/digits/hangup webhooks - kept
separate from outbound.py's AI-campaign flow on purpose. That pipeline
always opens the AI relay's <Stream>; this one never does. A COD
confirm/decline is a fixed yes/no, not a conversation, so the answer_url
here returns a plain Plivo <GetDigits> menu (shared/channels/voice/xml.py)
and the digits are resolved deterministically through the same core
shared/care/cod_confirmation.py::resolve_and_apply already uses for the
WhatsApp button-tap path.

Registered in services/voice/main.py like every other voice router; each
route also needs its own explicit forward in services/api/voice_proxy.py,
since Plivo only ever calls the one public API domain that proxies to the
real voice process - no wildcard passthrough exists there.
"""

import uuid

from fastapi import APIRouter, Header, Request, Response, status

from shared.channels.voice.plivo_signature import InvalidSignature, verify
from shared.channels.voice.xml import getdigits_response, hangup_response
from shared.config.settings import settings
from shared.db.models import Business, Customer, Order
from shared.db.session import AsyncSessionLocal
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()

_PROMPT = "Press 1 to confirm your cash on delivery order. Press 2 to cancel it."


async def _verify_form_or_403(request: Request, path: str, signature: str | None, nonce: str | None) -> dict | Response:
    """
    Reads the POST body and verifies against its real form params - Plivo
    signs whatever params are actually present (see plivo_signature.py's
    own empty_post_params branch), so a call answer's own metadata
    (CallUUID, To, From, ...) must be included here exactly like
    outbound.py's outbound_answer does, not verified against an empty set.
    Returns the parsed params dict on success, a 403 Response on failure.
    """
    body = await request.form()
    params = {k: str(v) for k, v in body.items()}
    try:
        verify(
            uri=f"{settings.public_base_url.rstrip('/')}{path}?{request.url.query}",
            signature=signature, nonce=nonce, method="POST", params=params,
        )
    except InvalidSignature:
        logger.warning("rejected %s - bad plivo signature", path)
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    return params


@router.post("/voice/cod-answer")
async def cod_answer(
    order_id: uuid.UUID,
    request: Request,
    x_plivo_signature_ma_v3: str | None = Header(default=None),
    x_plivo_signature_v3_nonce: str | None = Header(default=None),
) -> Response:
    """Plivo hits this once the call connects. Same Ma-V3 verification as
    every other Plivo callback in this codebase - see outbound.py."""
    result = await _verify_form_or_403(
        request, "/voice/cod-answer", x_plivo_signature_ma_v3, x_plivo_signature_v3_nonce,
    )
    if isinstance(result, Response):
        return result

    base = settings.public_base_url.rstrip("/")
    return Response(
        content=getdigits_response(
            _PROMPT,
            f"{base}/voice/cod-digits?order_id={order_id}",
            on_no_input="<Hangup/>",
        ),
        media_type="application/xml",
    )


@router.post("/voice/cod-digits")
async def cod_digits(
    order_id: uuid.UUID,
    request: Request,
    x_plivo_signature_ma_v3: str | None = Header(default=None),
    x_plivo_signature_v3_nonce: str | None = Header(default=None),
) -> Response:
    """Where the pressed digit lands. "1" confirms, "2" declines - anything
    else (including no digits, which Plivo never even posts here per
    <GetDigits>'s own retries-exhausted behavior) just hangs up, and the
    order is picked up by cod_call_failsafe.escalate_no_outcome later."""
    result = await _verify_form_or_403(
        request, "/voice/cod-digits", x_plivo_signature_ma_v3, x_plivo_signature_v3_nonce,
    )
    if isinstance(result, Response):
        return result

    digits = (result.get("Digits") or "").strip()

    async with AsyncSessionLocal() as db:
        order = await db.get(Order, order_id)
        if order is None or order.customer_id is None:
            await db.commit()
            return Response(content=hangup_response("order not found"), media_type="application/xml")

        if digits in ("1", "2"):
            from shared.care import cod_confirmation

            customer = await db.get(Customer, order.customer_id)
            business = await db.get(Business, order.business_id)
            if customer is not None and business is not None:
                await cod_confirmation.resolve_and_apply(
                    customer=customer, business=business,
                    confirmed=(digits == "1"), channel="voice", db=db,
                )
        await db.commit()

    return Response(content=hangup_response(), media_type="application/xml")


@router.post("/voice/cod-hangup")
async def cod_hangup(
    order_id: uuid.UUID,
    request: Request,
    x_plivo_signature_ma_v3: str | None = Header(default=None),
    x_plivo_signature_v3_nonce: str | None = Header(default=None),
) -> dict:
    """Plivo's hangup callback - nothing to record beyond what cod-digits
    (an answer) or cod_call_failsafe.escalate_no_outcome (no answer) already
    handle; this exists only because make_call requires a hangup_url."""
    result = await _verify_form_or_403(
        request, "/voice/cod-hangup", x_plivo_signature_ma_v3, x_plivo_signature_v3_nonce,
    )
    return {"received": not isinstance(result, Response)}
