"""
Calls a person makes or takes, as opposed to the ones the AI conducts.

Click-to-call: a team member taps Call on a customer. We ring *their* phone first; when
they pick up they hear who they are about to speak to, and only then is the customer
dialled - from the business's number, so the customer sees a number they recognise and a
member never gives out their own. Three webhooks Plivo calls:

  /voice/bridge-answer  the member picked up -> announce, then dial the customer
  /voice/bridge-dialed  the customer leg finished (answered or not)
  /voice/bridge-hangup  the call is over -> record how long, and what it cost

A call transferred to a member (see answer.py's transfer-xml) shares the cost step below,
because the leg to the member is a second leg Plivo bills and nothing else records it.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Header, Request, Response, status
from sqlalchemy import select

from shared.billing import usage
from shared.channels.voice.plivo_signature import InvalidSignature, verify
from shared.channels.voice.xml import bridge_response, customer_unreachable_response, hangup_response
from shared.config.settings import settings
from shared.auth.encryption import decrypt
from shared.db.models import Call, ChannelConnection, CustomerIdentity, IdentityKind, UsageEventType
from shared.db.session import AsyncSessionLocal
from shared.team import briefing
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()

_tasks: set[asyncio.Task] = set()


def _detach(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _spoken(text: str) -> str:
    """Plain words for a text-to-speech line: no symbols that would be read out."""
    cleaned = "".join(ch if ch.isalnum() or ch in " .,'-" else " " for ch in text)
    return " ".join(cleaned.split())[:240]


async def _customer_phone(db, customer_id: uuid.UUID) -> str | None:
    row = await db.execute(
        select(CustomerIdentity.value).where(
            CustomerIdentity.customer_id == customer_id, CustomerIdentity.kind == IdentityKind.phone,
        ).limit(1)
    )
    return row.scalar_one_or_none()


def _check(request: Request, path: str, params: dict, signature, nonce) -> bool:
    try:
        verify(
            uri=f"{settings.public_base_url.rstrip('/')}{path}?{request.url.query}",
            signature=signature, nonce=nonce, method="POST", params=params,
        )
        return True
    except InvalidSignature:
        logger.warning("rejected %s - bad plivo signature", path)
        return False


@router.post("/voice/bridge-answer")
async def bridge_answer(
    request: Request,
    call: uuid.UUID,
    x_plivo_signature_ma_v3: str | None = Header(default=None),
    x_plivo_signature_v3_nonce: str | None = Header(default=None),
) -> Response:
    body = await request.form()
    params = {k: str(v) for k, v in body.items()}
    if not _check(request, "/voice/bridge-answer", params, x_plivo_signature_ma_v3, x_plivo_signature_v3_nonce):
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    async with AsyncSessionLocal() as db:
        row = await db.get(Call, call)
        if row is None or row.customer_id is None or row.connection_id is None:
            return Response(content=hangup_response("call record missing"), media_type="application/xml")
        connection = await db.get(ChannelConnection, row.connection_id)
        number = await _customer_phone(db, row.customer_id)
        if connection is None or not number:
            return Response(content=hangup_response("no number to dial"), media_type="application/xml")

        row.external_id = str(body.get("CallUUID") or row.external_id)
        row.answered_at = datetime.now(timezone.utc)

        line = "Connecting you now."
        try:
            found = await briefing.build(db, business_id=row.business_id, customer_id=row.customer_id)
            if found is not None:
                line = f"Calling {found.headline}. Connecting you now."
        except Exception:
            logger.exception("could not build the call announcement call=%s", call)
        await db.commit()
        caller_id = "".join(ch for ch in connection.external_account_id if ch.isdigit())

    action = f"{settings.public_base_url.rstrip('/')}/voice/bridge-dialed?{urlencode({'call': str(call)})}"
    return Response(
        content=bridge_response(announce=_spoken(line), number=number, caller_id=caller_id, action_url=action),
        media_type="application/xml",
    )


@router.post("/voice/bridge-dialed")
async def bridge_dialed(
    request: Request,
    call: uuid.UUID,
    x_plivo_signature_ma_v3: str | None = Header(default=None),
    x_plivo_signature_v3_nonce: str | None = Header(default=None),
) -> Response:
    body = await request.form()
    params = {k: str(v) for k, v in body.items()}
    if not _check(request, "/voice/bridge-dialed", params, x_plivo_signature_ma_v3, x_plivo_signature_v3_nonce):
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    outcome = str(body.get("DialStatus") or "")
    leg = str(body.get("DialBLegUUID") or "")
    async with AsyncSessionLocal() as db:
        row = await db.get(Call, call)
        if row is not None:
            row.hangup_cause = outcome[:50] or row.hangup_cause
            if leg:
                row.cost_breakdown = {**(row.cost_breakdown or {}), "b_leg_uuid": leg}
        await db.commit()
    if outcome == "completed":
        return Response(content=hangup_response("done"), media_type="application/xml")
    return Response(content=customer_unreachable_response(), media_type="application/xml")


@router.post("/voice/bridge-hangup")
async def bridge_hangup(
    request: Request,
    call: uuid.UUID,
    x_plivo_signature_ma_v3: str | None = Header(default=None),
    x_plivo_signature_v3_nonce: str | None = Header(default=None),
) -> dict:
    body = await request.form()
    params = {k: str(v) for k, v in body.items()}
    if not _check(request, "/voice/bridge-hangup", params, x_plivo_signature_ma_v3, x_plivo_signature_v3_nonce):
        return {"ok": False}

    async with AsyncSessionLocal() as db:
        row = await db.get(Call, call)
        if row is not None and row.ended_at is None:
            row.ended_at = datetime.now(timezone.utc)
            try:
                row.duration_seconds = int(float(body.get("Duration") or 0))
            except ValueError:
                row.duration_seconds = 0
            if not row.hangup_cause:
                row.hangup_cause = str(body.get("HangupCauseName") or body.get("HangupCause") or "")[:50] or None
            # The member never picked up: nothing to write up, and no customer was dialled.
            if row.answered_at is None:
                row.wrapped_up_at = row.ended_at
        await db.commit()
    _detach(record_leg_costs(call))
    return {"ok": True}


# ── cost: every Plivo leg of a human call ───────────────────────────────────

async def record_leg_costs(call_id: uuid.UUID, *, only_b_leg: bool = False) -> None:
    """
    Add the Plivo cost of the human legs of this call to the usage ledger (the wallet pays
    cost + 25% from it). A click-to-call has two legs (the member's phone, then the
    customer's); a transferred call has one extra leg, to the member.

    Plivo's CDR is not queryable for a while after hangup, so this retries and, failing
    that, estimates from the number's rate - the same approach as the AI call's own cost.
    """
    from shared.channels.voice import relay  # heavy import, only needed here

    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(Call, call_id)
            if row is None:
                return
            legs: list[str] = []
            if not only_b_leg and row.external_id and not row.external_id.startswith("pending-"):
                legs.append(row.external_id)
            b_leg = (row.cost_breakdown or {}).get("b_leg_uuid")
            if b_leg:
                legs.append(b_leg)
            if not legs or (row.cost_breakdown or {}).get("human_cost_recorded"):
                return
            connection = await db.get(ChannelConnection, row.connection_id) if row.connection_id else None
            extra = (connection.extra or {}) if connection else {}
            sub_id = extra.get("subaccount_auth_id")
            sub_token = decrypt(connection.access_token) if connection and connection.access_token and sub_id else None
            voice_rate = extra.get("voice_rate")
            business_id = row.business_id
            duration = row.duration_seconds or 0

        total = 0
        minutes = 0.0
        source = "cdr"
        for leg in legs:
            cdr = await relay._fetch_plivo_cdr(sub_id, sub_token, leg)
            if cdr is None:
                source = "estimate"
                total += relay._estimate_plivo_paise(duration, float(voice_rate) if voice_rate else None)
                minutes += duration / 60
            else:
                total += round(float(cdr.get("total_amount", 0)) * relay._USD_TO_INR * 100)
                minutes += float(cdr.get("billed_duration") or duration) / 60

        async with AsyncSessionLocal() as db:
            row = await db.get(Call, call_id)
            if row is None:
                return
            breakdown = dict(row.cost_breakdown or {})
            if breakdown.get("human_cost_recorded"):
                return
            breakdown["human_cost_recorded"] = True
            # Its own key: an AI call's own cost step writes plivo_voice_paise and must not be trodden on.
            breakdown["human_leg_paise"] = total
            breakdown["human_leg_source"] = source
            row.cost_breakdown = breakdown
            row.cost_paise = int(row.cost_paise or 0) + total
            usage.record(
                business_id=business_id, event_type=UsageEventType.voice_call_minutes, channel="voice",
                quantity=minutes, unit="minute", krova_cost_paise=total,
                source_type="call", source_id=call_id, extra={"cost_source": source, "human": True}, db=db,
            )
            await db.commit()
    except Exception:
        logger.exception("could not record the cost of human call %s", call_id)
