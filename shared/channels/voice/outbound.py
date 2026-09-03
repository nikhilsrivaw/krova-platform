"""
Placing outbound calls for a call campaign, and building what the live
pipeline needs once one connects.

The AI-answered conversation itself is not reimplemented here - once a
call connects, relay.py drives the exact same CallPipeline (barge-in,
reply generation, booking, transfer) any inbound call already uses. This
module owns only what's genuinely different about an outbound call:
placing it, and building its opening line + route from a known
CallCampaignRecipient rather than a dialled phone number.

Correlation is by recipient_id in the URL, not by call_uuid: Plivo's Make
Call API only returns a request_uuid at the moment of placing a call, not
the eventual call_uuid (that only exists once Plivo actually dials out,
and only ever arrives later - as CallUUID - inside the answer_url/
hangup_url payloads themselves). So nothing here can key anything on
call_uuid in advance; recipient_id travels in the webhook URLs' own query
string instead, resolved fresh from the database on each callback rather
than relying on any in-process stash.

Real-world note, not enforced here: automated outbound calling in India
needs a business phone number in the right TRAI-registered series (140
promotional, 160 BFSI-only transactional/service) and DLT registration -
neither is Plivo's normal searchable/instant-buy inventory (confirmed:
nothing listed under 160 in Plivo's own number search), and 160-series
doesn't apply to most of KROVA's actual customer base (not BFSI). This
module places calls on whatever voice number IS connected to a business;
getting a compliant number connected is a real-world provisioning step,
not this module's job.
"""

import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Header, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.ai import context as agent_context
from shared.ai import outbound_opener
from shared.auth.encryption import decrypt
from shared.billing import usage
from shared.channels.voice import plivo_client
from shared.channels.voice.call_registry import remember
from shared.channels.voice.plivo_signature import InvalidSignature, verify
from shared.channels.voice.tenant import VoiceRoute, resolve_by_business
from shared.channels.voice.xml import hangup_response, stream_response
from shared.config.settings import settings
from shared.db.models import (
    CallCampaign,
    CallCampaignRecipient,
    CallCampaignRecipientStatus,
    Channel,
    ChannelConnection,
    ConnectionStatus,
    CustomerIdentity,
    IdentityKind,
    UsageEventType,
)
from shared.db.session import AsyncSessionLocal
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


class OutboundCallError(Exception):
    """Could not place this call. The recipient's own status/reason records why."""


@dataclass(slots=True)
class OutboundCallContext:
    """Everything relay.py's stream() needs to drive an outbound call, once Plivo connects it."""

    route: VoiceRoute
    customer_id: uuid.UUID
    customer_phone: str
    opening_line: str


async def _customer_phone(customer_id: uuid.UUID, db: AsyncSession) -> str | None:
    result = await db.execute(
        select(CustomerIdentity.value).where(
            CustomerIdentity.customer_id == customer_id,
            CustomerIdentity.kind == IdentityKind.phone,
        )
    )
    return result.scalars().first()


async def build_context(recipient_id: uuid.UUID, db: AsyncSession) -> OutboundCallContext | None:
    """
    Called from /voice/outbound-answer once a human picks up - resolves
    the route and drafts the opening line from the campaign's own
    objective. Returns None for anything that means this call should not
    proceed (recipient/campaign vanished, no phone on file, voice
    connection gone) - the caller hangs up cleanly rather than guessing.
    """
    recipient = await db.get(CallCampaignRecipient, recipient_id)
    if recipient is None:
        return None
    campaign = await db.get(CallCampaign, recipient.call_campaign_id)
    if campaign is None:
        return None

    route = await resolve_by_business(campaign.business_id, db)
    if route is None:
        return None

    phone = await _customer_phone(recipient.customer_id, db)
    if not phone:
        return None

    context = await agent_context.build(campaign.business_id, recipient.customer_id, db)
    opener = await outbound_opener.draft(context, reason=campaign.objective)
    if opener.cost_paise:
        usage.record(
            business_id=campaign.business_id,
            event_type=UsageEventType.ai_reply_generated,
            channel="voice",
            quantity=1,
            unit="call",
            krova_cost_paise=opener.cost_paise,
            source_type="call_campaign_recipient",
            source_id=recipient.id,
            db=db,
        )

    return OutboundCallContext(
        route=route,
        customer_id=recipient.customer_id,
        customer_phone=phone,
        opening_line=opener.text,
    )


async def place_call(recipient_id: uuid.UUID, db: AsyncSession) -> None:
    """
    Dial one campaign recipient. Marks the recipient `calling` on success,
    `failed`/`skipped` (with why) on anything that stops the call from
    being placed at all - a Plivo error, a missing phone number, a
    disconnected voice channel. What happens AFTER the call connects
    (answered, voicemail, no answer) is recorded separately, by
    /voice/outbound-answer and /voice/outbound-hangup below - this
    function's job ends the moment the call is either fired or fails to
    fire. Caller commits.
    """
    recipient = await db.get(CallCampaignRecipient, recipient_id)
    if recipient is None:
        return
    campaign = await db.get(CallCampaign, recipient.call_campaign_id)
    if campaign is None:
        recipient.status = CallCampaignRecipientStatus.failed
        recipient.reason = "Campaign no longer exists"
        return

    to_number = await _customer_phone(recipient.customer_id, db)
    if not to_number:
        recipient.status = CallCampaignRecipientStatus.skipped
        recipient.reason = "No phone number on file"
        return

    connection = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == campaign.business_id,
                ChannelConnection.channel == Channel.voice,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()
    if connection is None or not connection.access_token:
        recipient.status = CallCampaignRecipientStatus.failed
        recipient.reason = "No voice number connected"
        return

    auth_id = (connection.extra or {}).get("subaccount_auth_id")
    if not auth_id:
        recipient.status = CallCampaignRecipientStatus.failed
        recipient.reason = "Voice connection is missing its subaccount id"
        return

    base = settings.public_base_url.rstrip("/")
    try:
        await plivo_client.make_call(
            auth_id=auth_id,
            auth_token=decrypt(connection.access_token),
            from_number=connection.external_account_id,
            to_number=to_number,
            answer_url=f"{base}/voice/outbound-answer?recipient_id={recipient.id}",
            hangup_url=f"{base}/voice/outbound-hangup?recipient_id={recipient.id}",
        )
    except plivo_client.PlivoError as exc:
        recipient.status = CallCampaignRecipientStatus.failed
        recipient.reason = str(exc)
        logger.warning("outbound call failed to place recipient=%s: %s", recipient.id, exc)
        return

    recipient.status = CallCampaignRecipientStatus.calling
    logger.info("outbound call placed recipient=%s to=%s", recipient.id, to_number)


# ── webhooks ─────────────────────────────────────────────────────────────

@router.post("/voice/outbound-answer")
async def outbound_answer(
    recipient_id: uuid.UUID,
    request: Request,
    x_plivo_signature_ma_v3: str | None = Header(default=None),
    x_plivo_signature_v3_nonce: str | None = Header(default=None),
) -> Response:
    """
    Plivo hits this once a human picks up an outbound call - never fired
    at all when machine_detection="hangup" catches a machine, since Plivo
    hangs up before ever reaching an answer_url in that case (see
    outbound_hangup below, where that outcome is actually recorded).

    Verified the same way /voice/answer already is - Ma-V3, Krova's own
    parent token, regardless of which business's subaccount placed the
    call. Signed over the raw received query string (request.url.query),
    not a rebuilt one, so nothing here can mismatch what Plivo actually
    signed - same reasoning as /voice/transfer-xml.
    """
    body = await request.form()
    params = {k: str(v) for k, v in body.items()}

    try:
        verify(
            uri=f"{settings.public_base_url.rstrip('/')}/voice/outbound-answer?{request.url.query}",
            signature=x_plivo_signature_ma_v3,
            nonce=x_plivo_signature_v3_nonce,
            method="POST",
            params=params,
        )
    except InvalidSignature:
        logger.warning("rejected /voice/outbound-answer - bad plivo signature")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    call_uuid = body.get("CallUUID") or body.get("callId")
    to_number = body.get("To") or body.get("to")
    from_number = body.get("From") or body.get("from")
    if call_uuid:
        # Same stash relay.py's inbound flow already reads from - not used
        # for outbound routing (recipient_id in the URL covers that), only
        # so the eventual /voice/stream logging has real to/from numbers
        # to show, the same as an inbound call would.
        remember(str(call_uuid), to_number=str(to_number or ""), from_number=str(from_number or ""))

    async with AsyncSessionLocal() as db:
        context = await build_context(recipient_id, db)
        await db.commit()

    if context is None:
        logger.warning("outbound-answer for recipient=%s could not build a call context", recipient_id)
        return Response(content=hangup_response("could not build outbound call context"), media_type="application/xml")

    ws_url = (
        f"{settings.public_base_url.replace('https://', 'wss://')}"
        f"/voice/stream?recipient_id={recipient_id}"
    )
    return Response(
        content=stream_response(ws_url, status_callback_url=f"{settings.public_base_url}/voice/status"),
        media_type="application/xml",
    )


@router.post("/voice/outbound-hangup")
async def outbound_hangup(
    recipient_id: uuid.UUID,
    request: Request,
    x_plivo_signature_ma_v3: str | None = Header(default=None),
    x_plivo_signature_v3_nonce: str | None = Header(default=None),
) -> dict:
    """
    Plivo's outbound hangup callback - fires for every outbound call
    regardless of outcome, including the synchronous-AMD case where a
    machine was detected and the call never reached outbound_answer at
    all. `machine` == "true" is Plivo's own confirmed field for that case
    (docs.plivo confirmed, not guessed) - checked here rather than assumed
    from any other field.
    """
    body = await request.form()
    params = {k: str(v) for k, v in body.items()}

    try:
        verify(
            uri=f"{settings.public_base_url.rstrip('/')}/voice/outbound-hangup?{request.url.query}",
            signature=x_plivo_signature_ma_v3,
            nonce=x_plivo_signature_v3_nonce,
            method="POST",
            params=params,
        )
    except InvalidSignature:
        logger.warning("rejected /voice/outbound-hangup - bad plivo signature")
        return {"received": False}

    is_machine = (body.get("machine") or "").lower() == "true"

    async with AsyncSessionLocal() as db:
        recipient = await db.get(CallCampaignRecipient, recipient_id)
        if recipient is not None and recipient.status == CallCampaignRecipientStatus.calling:
            recipient.status = (
                CallCampaignRecipientStatus.voicemail if is_machine
                else CallCampaignRecipientStatus.completed
            )
            logger.info(
                "outbound call finished recipient=%s outcome=%s",
                recipient_id, recipient.status.value,
            )
        await db.commit()

    return {"received": True}
