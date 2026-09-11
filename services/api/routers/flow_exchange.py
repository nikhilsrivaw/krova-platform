"""
The WhatsApp Flow data_exchange endpoint - what Meta calls mid-flow.

Root-mounted (no /api/v1 prefix, no JWT) for the same reason webhooks.py
is: this URL is registered with Meta itself as a flow's endpoint_uri, not
something a business's own frontend calls. Authenticity comes from
shared/channels/whatsapp/flow_encryption.py's RSA/AES scheme, not a
bearer token - Meta signs the app-level request too
(X-Hub-Signature-256), reusing shared/channels/whatsapp/signature.py
exactly as the inbound message webhook already does.

Currently wired for exactly one real capability: real availability for a
"book a follow-up" style flow (see clinic.json's book_followup_live
template) - screen 1 collects name/phone/reason and data_exchanges for
real open slots (shared/scheduling/availability.open_slots, the same
function voice/WhatsApp text booking already uses); screen 2 shows only
slots that are actually open, and its own data_exchange performs the
real booking (shared/scheduling/booking.book) before returning SUCCESS -
never a blind "complete" trusting the client's own claim.

A data_exchange booking has no natural source Message to cite (the
customer never wrote a message, they filled a form) - source_message_ids
is a hard requirement for a non-manual booking, matching every other
channel's own provenance guarantee. Resolved by writing one real,
synthetic inbound Message the moment the form is filled ("Completed a
form: ..."), the same shape a static flow's own nfm_reply already
produces - so the booking's citation is real and inspectable, not
invented.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from sqlalchemy import select

from shared.auth.encryption import decrypt as decrypt_credential
from shared.channels import ingest
from shared.channels.whatsapp import flow_encryption
from shared.channels.whatsapp.signature import InvalidSignature
from shared.channels.whatsapp.signature import verify as verify_signature
from shared.db.models import (
    Business,
    Channel,
    ChannelConnection,
    ConnectionStatus,
    Customer,
    CustomerIdentity,
    Direction,
    Doctor,
    FlowSendLog,
    IdentityKind,
    IntakeChannel,
    Property,
    WhatsAppFlow,
)
from shared.db.session import AsyncSessionLocal
from shared.scheduling import availability, booking
from shared.scheduling.booking import SlotUnavailable
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/flows", tags=["flow-exchange"])


def _slot_label(starts_at: datetime, tz: ZoneInfo) -> str:
    local = starts_at.astimezone(tz)
    return local.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


async def _open_slot_options(business: Business, doctor: Doctor, db) -> list[dict]:
    """Real open slots, next 6 days, as a Flow data-source - id is the exact
    ISO start time, re-resolved (never trusted back from the client) when
    a selection comes in."""
    tz = ZoneInfo(business.timezone)
    now = datetime.now(tz)
    options: list[dict] = []
    for day_offset in range(6):
        on_date = (now + timedelta(days=day_offset)).date()
        slots = await availability.open_slots(db, business=business, doctor=doctor, on_date=on_date, now=now)
        for slot in slots[:4]:  # a handful per day - Meta's RadioButtonsGroup isn't meant for hundreds of options
            options.append({"id": slot.starts_at.isoformat(), "title": _slot_label(slot.starts_at, tz)})
        if len(options) >= 24:
            break
    return options


@router.post("/{flow_id}/exchange")
async def exchange(
    flow_id: uuid.UUID, request: Request, x_hub_signature_256: str | None = Header(default=None),
) -> Response:
    """
    Meta's own data_exchange call. Every response is 200 with an encrypted
    body except for a decryption failure, which Meta requires to be a
    plain 421 - see shared/channels/whatsapp/flow_encryption.py's own
    docstring for the crypto itself.
    """
    raw_body = await request.body()
    try:
        verify_signature(raw_body, x_hub_signature_256)
    except InvalidSignature:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid signature")

    envelope = json.loads(raw_body)

    async with AsyncSessionLocal() as db:
        flow = await db.get(WhatsAppFlow, flow_id)
        if flow is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown flow")

        connection = (
            await db.execute(
                select(ChannelConnection).where(
                    ChannelConnection.business_id == flow.business_id,
                    ChannelConnection.channel == Channel.whatsapp,
                    ChannelConnection.status == ConnectionStatus.active,
                )
            )
        ).scalars().first()
        keys = (connection.extra or {}).get("flow_encryption") if connection else None
        if not keys or not keys.get("private_key"):
            raise HTTPException(status.HTTP_421_MISDIRECTED_REQUEST, "Encryption not configured for this flow")

        try:
            private_key_pem = decrypt_credential(keys["private_key"])
            body, aes_key, iv = flow_encryption.decrypt_request(
                private_key_pem,
                envelope["encrypted_flow_data"], envelope["encrypted_aes_key"], envelope["initial_vector"],
            )
        except Exception:
            logger.exception("flow exchange decryption failed flow=%s", flow_id)
            return Response(status_code=421)

        action = body.get("action")
        if action == "ping":
            reply = {"data": {"status": "active"}}
        elif action == "INIT":
            reply = {"screen": (flow.flow_json.get("screens") or [{}])[0].get("id", ""), "data": {}}
        elif action == "data_exchange":
            try:
                reply = await _handle_data_exchange(flow, connection, body, db)
                await db.commit()
            except Exception:
                logger.exception("flow exchange data_exchange handling failed flow=%s", flow_id)
                await db.rollback()
                reply = {"data": {"error_message": "Something went wrong - please try again."}}
        else:
            reply = {"data": {"acknowledged": True}}

        encrypted = flow_encryption.encrypt_response(reply, aes_key, iv)
        return Response(content=encrypted, media_type="text/plain")


async def _handle_data_exchange(flow, connection: ChannelConnection, body: dict, db) -> dict:
    screen = body.get("screen")
    data = body.get("data") or {}
    flow_token = body.get("flow_token")

    send_log = (
        await db.execute(
            select(FlowSendLog).where(FlowSendLog.business_id == flow.business_id, FlowSendLog.flow_token == flow_token)
        )
    ).scalars().first()
    if send_log is None:
        return {"data": {"error_message": "This form has expired - ask to be sent it again."}}

    business = await db.get(Business, flow.business_id)
    customer = await db.get(Customer, send_log.customer_id)
    doctor = (
        await db.execute(
            select(Doctor).where(Doctor.business_id == flow.business_id, Doctor.active == True)  # noqa: E712
        )
    ).scalars().first()
    if business is None or customer is None or doctor is None:
        return {"data": {"error_message": "This business isn't set up for live booking yet."}}

    # Whatever the entry screen collected besides the slot itself - reason
    # (clinic), property (real estate), time_of_day, ... - carried through
    # both screens verbatim rather than hardcoding one vertical's field
    # names, so the same handler serves every live-booking flow template.
    passthrough = {k: str(v) for k, v in data.items() if k != "selected_slot" and v is not None}

    if screen == "SELECT_SLOT":
        selected = data.get("selected_slot")
        try:
            requested = datetime.fromisoformat(selected) if selected else None
        except ValueError:
            requested = None

        # Re-resolved against the real calendar, never trusted from the
        # client's own claimed id - the same discipline
        # try_book_from_agent already applies to an AI-suggested slot.
        # The business's own local date, not requested.date() directly -
        # Slot.starts_at is UTC, and a slot near midnight local time can
        # fall on a different UTC calendar date (open_slots itself is
        # queried by local date, so matching on the wrong one would find
        # nothing even for a real, still-open slot).
        matched = None
        if requested is not None:
            local_date = requested.astimezone(ZoneInfo(business.timezone)).date()
            for candidate in await availability.open_slots(db, business=business, doctor=doctor, on_date=local_date):
                if candidate.starts_at == requested:
                    matched = candidate
                    break
        if matched is None:
            fresh = await _open_slot_options(business, doctor, db)
            return {
                "screen": "SELECT_SLOT",
                "data": {**passthrough, "available_slots": fresh,
                          "error_message": "That slot was just taken - please pick another."},
            }

        # Real estate only - resolved by name against active listings, the
        # same match-or-proceed-unlinked logic try_book_from_agent applies
        # to an AI-suggested property name. A customer-typed name that
        # doesn't match still lets the booking through (unlinked) rather
        # than blocking it - a Flow field is more error-prone than an
        # agent's own resolution, and the appointment itself is what
        # matters most.
        property_id = None
        property_name = passthrough.get("property", "").strip()
        if property_name:
            properties = (
                await db.execute(
                    select(Property).where(Property.business_id == flow.business_id, Property.active == True)  # noqa: E712
                )
            ).scalars().all()
            match = next((p for p in properties if p.title.strip().lower() == property_name.lower()), None)
            if match is not None:
                property_id = match.id

        # The customer's own confirmed WhatsApp identity, not the form's
        # phone field - guarantees ingest() attaches this message to the
        # SAME customer the flow was actually sent to, rather than
        # resolving (or worse, creating) a different one from whatever a
        # customer typed into the form.
        real_phone = (
            await db.execute(
                select(CustomerIdentity.value).where(
                    CustomerIdentity.customer_id == customer.id, CustomerIdentity.kind == IdentityKind.phone,
                )
            )
        ).scalars().first()
        if real_phone is None:
            return {"data": {"error_message": "Something went wrong - please try again."}}

        details = ", ".join(f"{k}: {v}" for k, v in passthrough.items() if v)
        summary = f"Completed a form: {details}, preferred_date: {matched.starts_at.isoformat()}"
        stored = await ingest.ingest(
            business_id=flow.business_id, channel=Channel.whatsapp, direction=Direction.inbound,
            identity_kind=IdentityKind.phone, identity_value=real_phone,
            external_id=f"flow-exchange:{flow_token}", text=summary,
            occurred_at=datetime.now(timezone.utc), connection_id=connection.id,
            media={"kind": "flow_reply", "flow_name": flow.name},
            enqueue_analysis=False, db=db,
        )
        if stored.message is None:
            return {"data": {"error_message": "Something went wrong recording your request - please try again."}}

        notes = passthrough.get("reason") or None

        try:
            await booking.book(
                db, business_id=flow.business_id, doctor=doctor, customer=customer, slot=matched,
                intake_channel=IntakeChannel.whatsapp, source_message_ids=[stored.message.id],
                notes=notes, property_id=property_id,
            )
        except SlotUnavailable:
            fresh = await _open_slot_options(business, doctor, db)
            return {
                "screen": "SELECT_SLOT",
                "data": {**passthrough, "available_slots": fresh,
                          "error_message": "That slot was just taken - please pick another."},
            }

        return {"screen": "SUCCESS", "data": {"extension_message_response": {"params": {"flow_token": flow_token}}}}

    # Default: any other entry screen (book_followup_live's name/phone/
    # reason, schedule_viewing_live's name/phone/property/time_of_day, a
    # future vertical's own fields) - offer real slots, carrying whatever
    # was collected forward untouched.
    options = await _open_slot_options(business, doctor, db)
    return {
        "screen": "SELECT_SLOT",
        "data": {**passthrough, "available_slots": options or [{"id": "none", "title": "No slots open right now"}]},
    }
