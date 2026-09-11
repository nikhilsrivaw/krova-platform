"""
WhatsApp Flows: authoring, publishing, and sending native in-chat forms.

Three lifecycle steps map onto three endpoints - create (uploads the Flow
JSON, does not publish), publish (makes it sendable), send (opens it for one
customer). Kept separate rather than one "create and publish" call because a
flow with validation errors should be visible and fixable before it can ever
reach a real customer, not published sight-unseen.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared import verticals
from shared.auth.encryption import decrypt, encrypt
from shared.channels import ingest
from shared.channels.whatsapp import flow_encryption, flows as flows_api
from shared.channels.whatsapp.client import (
    WhatsAppClient,
    WhatsAppError,
    within_service_window,
)
from shared.db.models import (
    Business,
    Channel,
    ChannelConnection,
    ConnectionStatus,
    Customer,
    CustomerIdentity,
    Direction,
    FlowSendLog,
    FlowStatus,
    IdentityKind,
    Message,
    WhatsAppFlow,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/flows", tags=["flows"])


async def _connection(business_id: uuid.UUID, db: DbDep) -> ChannelConnection:
    result = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == business_id,
            ChannelConnection.channel == Channel.whatsapp,
            ChannelConnection.status == ConnectionStatus.active,
        )
    )
    connection = result.scalars().first()
    if connection is None or not connection.access_token:
        raise HTTPException(status.HTTP_409_CONFLICT, "Connect WhatsApp first")
    return connection


async def _waba_id(connection: ChannelConnection) -> str:
    waba_id = (connection.extra or {}).get("waba_id")
    if not waba_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This WhatsApp connection has no WhatsApp Business Account on record - reconnect it",
        )
    return waba_id


class FlowIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    categories: list[str] = Field(min_length=1)
    flow_json: dict = Field(description="The Flow JSON defining this flow's screens")


class FlowOut(BaseModel):
    id: str
    meta_flow_id: str
    name: str
    categories: list[str]
    status: str
    validation_errors: list[dict]
    flow_json: dict


def _out(f: WhatsAppFlow) -> FlowOut:
    return FlowOut(
        id=str(f.id),
        meta_flow_id=f.meta_flow_id,
        name=f.name,
        categories=f.categories or [],
        status=f.status.value if hasattr(f.status, "value") else str(f.status),
        validation_errors=f.validation_errors or [],
        flow_json=f.flow_json,
    )


@router.get("", response_model=list[FlowOut])
async def list_flows(current_user: CurrentUserDep, db: DbDep) -> list[FlowOut]:
    rows = await db.execute(
        select(WhatsAppFlow).where(WhatsAppFlow.business_id == current_user.business)
    )
    return [_out(f) for f in rows.scalars().all()]


class FlowTemplateOut(BaseModel):
    key: str
    name: str
    description: str
    categories: list[str]
    cta: str
    body: str
    screen: str
    flow_json: dict


@router.get("/templates", response_model=list[FlowTemplateOut])
async def list_flow_templates(current_user: CurrentUserDep, db: DbDep) -> list[FlowTemplateOut]:
    """
    Ready-made Flow JSON for this business's own vertical - a starting point
    to lightly edit and publish, not a fixed builder (shared/verticals/
    __init__.py's own stated philosophy: a template per vertical, not an
    empty canvas). Registered before /{flow_id} so "templates" is never
    swallowed as a flow id.

    Empty for a vertical that doesn't have one yet - that's an honest gap,
    not a bug to hide behind a fallback.
    """
    business = await db.get(Business, current_user.business)
    vertical_key = business.vertical if business else verticals.FALLBACK
    try:
        template = verticals.get(vertical_key)
    except verticals.UnknownVertical:
        return []
    return [FlowTemplateOut(**t) for t in template.get("flow_templates", [])]


@router.get("/{flow_id}", response_model=FlowOut)
async def get_flow(flow_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> FlowOut:
    flow = await db.get(WhatsAppFlow, flow_id)
    if flow is None or flow.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Flow not found")
    return _out(flow)


@router.post("", response_model=FlowOut, status_code=status.HTTP_201_CREATED)
async def create_flow(body: FlowIn, current_user: CurrentUserDep, db: DbDep) -> FlowOut:
    """
    Author a new flow: create it on Meta, upload its screens, and store what
    Meta made of it. Left in DRAFT - publish_flow is a separate, deliberate
    step so validation errors are visible before anything can be sent.
    """
    connection = await _connection(current_user.business, db)
    waba_id = await _waba_id(connection)
    token = decrypt(connection.access_token)

    try:
        meta_flow_id = await flows_api.create_flow(token, waba_id, body.name, body.categories)
        issues = await flows_api.upload_flow_json(token, meta_flow_id, body.flow_json)
    except flows_api.FlowError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    flow = WhatsAppFlow(
        business_id=current_user.business,
        meta_flow_id=meta_flow_id,
        name=body.name,
        categories=body.categories,
        status=FlowStatus.draft,
        flow_json=body.flow_json,
        validation_errors=[
            {"error_type": i.error_type, "message": i.message, "line_start": i.line_start, "line_end": i.line_end}
            for i in issues
        ],
    )
    db.add(flow)
    await db.flush()
    logger.info(
        "flow created business=%s meta_flow_id=%s issues=%d",
        current_user.business, meta_flow_id, len(issues),
    )
    return _out(flow)


@router.post("/{flow_id}/publish", response_model=FlowOut)
async def publish_flow(flow_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> FlowOut:
    flow = await db.get(WhatsAppFlow, flow_id)
    if flow is None or flow.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Flow not found")
    if flow.validation_errors:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This flow has validation errors and cannot be published - fix them and re-create it",
        )

    connection = await _connection(current_user.business, db)
    token = decrypt(connection.access_token)
    try:
        await flows_api.publish_flow(token, flow.meta_flow_id)
    except flows_api.FlowError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    flow.status = FlowStatus.published
    await db.flush()
    logger.info("flow published business=%s meta_flow_id=%s", current_user.business, flow.meta_flow_id)
    return _out(flow)


@router.post("/{flow_id}/deprecate", response_model=FlowOut)
async def deprecate_flow(flow_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> FlowOut:
    """
    Retire a published flow - found genuinely unwired during a wiring
    audit: flows_api.deprecate_flow (the Graph API call) has existed
    since this router was first built, with no endpoint ever calling it.
    Meta does not allow deleting a published flow outright; deprecating
    is the only way to stop it being sendable again.
    """
    flow = await db.get(WhatsAppFlow, flow_id)
    if flow is None or flow.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Flow not found")
    if flow.status != FlowStatus.published:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a published flow can be deprecated")

    connection = await _connection(current_user.business, db)
    token = decrypt(connection.access_token)
    try:
        await flows_api.deprecate_flow(token, flow.meta_flow_id)
    except flows_api.FlowError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    flow.status = FlowStatus.deprecated
    await db.flush()
    logger.info("flow deprecated business=%s meta_flow_id=%s", current_user.business, flow.meta_flow_id)
    return _out(flow)


@router.post("/{flow_id}/refresh", response_model=FlowOut)
async def refresh_flow(flow_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> FlowOut:
    """
    Re-read a flow's real status and validation state from Meta - also
    found unwired during a wiring audit (flows_api.get_flow_status
    existed, nothing called it). Meta can re-review or auto-deprecate a
    flow independently of anything done here, so KROVA's own copy can
    drift without this - never trusted as current on its own otherwise.
    """
    flow = await db.get(WhatsAppFlow, flow_id)
    if flow is None or flow.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Flow not found")

    connection = await _connection(current_user.business, db)
    token = decrypt(connection.access_token)
    try:
        info = await flows_api.get_flow_status(token, flow.meta_flow_id)
    except flows_api.FlowError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    try:
        flow.status = FlowStatus(info.status)
    except ValueError:
        logger.warning("flow refresh got unrecognised status %r flow=%s", info.status, flow.id)
    flow.validation_errors = [
        {"error_type": i.error_type, "message": i.message, "line_start": i.line_start, "line_end": i.line_end}
        for i in info.validation_errors
    ]
    await db.flush()
    return _out(flow)


class LiveDataOut(BaseModel):
    enabled: bool


@router.post("/{flow_id}/enable-live-data", response_model=LiveDataOut)
async def enable_live_data(flow_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> LiveDataOut:
    """
    Point this flow at KROVA's own data_exchange endpoint (see services/
    api/routers/flow_exchange.py) - what turns a static form into one that
    shows real data (right now: real open appointment slots) mid-flow.

    Registers (or reuses) one RSA keypair for the whole WhatsApp
    connection - confirmed against Meta's Business Encryption API that
    this is per phone number, not per flow, so a business only does this
    dance once no matter how many live flows it has.
    """
    flow = await db.get(WhatsAppFlow, flow_id)
    if flow is None or flow.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Flow not found")

    connection = await _connection(current_user.business, db)
    token = decrypt(connection.access_token)

    keys = (connection.extra or {}).get("flow_encryption")
    if not keys or not keys.get("public_key") or not keys.get("private_key"):
        public_pem, private_pem = flow_encryption.generate_keypair()
        keys = {"public_key": public_pem, "private_key": encrypt(private_pem)}
        connection.extra = {**(connection.extra or {}), "flow_encryption": keys}

    from shared.config.settings import settings

    try:
        await flows_api.register_public_key(token, connection.external_account_id, keys["public_key"])
        endpoint_uri = f"{settings.public_base_url.rstrip('/')}/flows/{flow.id}/exchange"
        await flows_api.set_data_endpoint(token, flow.meta_flow_id, endpoint_uri)
    except flows_api.FlowError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    await db.flush()
    logger.info("flow live-data enabled business=%s flow=%s", current_user.business, flow.id)
    return LiveDataOut(enabled=True)


class FlowSendIn(BaseModel):
    customer_id: str
    body: str = Field(min_length=1, max_length=1024)
    screen: str = Field(description="The entry screen id, as it appears in the Flow JSON")
    cta: str = Field(min_length=1, max_length=20, description="The button text that opens the flow")
    data: dict = Field(default_factory=dict)
    draft: bool = False


class FlowSendOut(BaseModel):
    sent: bool
    message_id: str
    flow_token: str


@router.post("/{flow_id}/send", response_model=FlowSendOut)
async def send_flow(
    flow_id: uuid.UUID, body: FlowSendIn, current_user: CurrentUserDep, db: DbDep
) -> FlowSendOut:
    flow = await db.get(WhatsAppFlow, flow_id)
    if flow is None or flow.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Flow not found")
    if flow.status != FlowStatus.published and not body.draft:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This flow is not published yet - publish it, or pass draft=true to test it as an app tester",
        )

    try:
        customer_uuid = uuid.UUID(body.customer_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid customer_id")
    customer = await db.get(Customer, customer_uuid)
    if customer is None or customer.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")

    identity_result = await db.execute(
        select(CustomerIdentity).where(
            CustomerIdentity.customer_id == customer_uuid,
            CustomerIdentity.kind == IdentityKind.phone,
        )
    )
    identity = identity_result.scalars().first()
    if identity is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This customer has no WhatsApp number on record")
    to = identity.value

    last_inbound = await db.execute(
        select(Message.occurred_at)
        .where(Message.customer_id == customer_uuid, Message.direction == Direction.inbound)
        .order_by(Message.occurred_at.desc())
        .limit(1)
    )
    if not within_service_window(last_inbound.scalars().first()):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The 24-hour window has closed - a flow cannot be sent as a template",
        )

    connection = await _connection(current_user.business, db)
    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)
    flow_token = str(uuid.uuid4())

    try:
        result = await client.send_flow_message(
            to,
            body.body,
            flow_id=flow.meta_flow_id,
            flow_token=flow_token,
            flow_cta=body.cta,
            screen=body.screen,
            data=body.data,
            draft=body.draft,
        )
    except WhatsAppError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    db.add(FlowSendLog(
        business_id=current_user.business,
        flow_id=flow.id,
        customer_id=customer_uuid,
        flow_token=flow_token,
    ))

    await ingest.ingest(
        business_id=current_user.business,
        channel=Channel.whatsapp,
        direction=Direction.outbound,
        identity_kind=IdentityKind.phone,
        identity_value=to,
        external_id=result.external_id,
        text=body.body,
        occurred_at=datetime.now(timezone.utc),
        connection_id=connection.id,
        raw={"flow_id": flow.meta_flow_id, "screen": body.screen, "flow_token": flow_token},
        media={"kind": "flow_open", "flow_name": flow.name},
        enqueue_analysis=False,
        sent_by_user_id=current_user.id,
        db=db,
    )

    logger.info(
        "flow sent business=%s flow=%s customer=%s token=%s",
        current_user.business, flow.meta_flow_id, customer_uuid, flow_token,
    )

    return FlowSendOut(sent=True, message_id=result.external_id, flow_token=flow_token)
