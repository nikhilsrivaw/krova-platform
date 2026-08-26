"""
Sending messages.

The 24-hour rule decides everything here. Inside the customer service window
a business can write freely; outside it, only an approved template delivers
and a free-form send is refused with error 131047.

So this router does not make the caller remember which situation they are in.
It looks at when the customer last wrote, picks the only thing that can work,
and says plainly when nothing can.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.auth.encryption import decrypt
from shared.channels import ingest
from shared.channels.whatsapp.client import (
    WhatsAppClient,
    WhatsAppError,
    within_service_window,
)
from shared.db.models import (
    Channel,
    ChannelConnection,
    ConnectionStatus,
    Customer,
    CustomerIdentity,
    Direction,
    IdentityKind,
    Message,
    MessageTemplate,
    TemplateStatus,
)
from shared.identity.normalise import InvalidIdentifier, normalise_phone
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/messages", tags=["messages"])


class SendText(BaseModel):
    to: str = Field(description="Phone number, any format")
    body: str = Field(min_length=1, max_length=4096)


class SendTemplate(BaseModel):
    to: str
    template_name: str
    language: str = "en"
    variables: list[str] = Field(
        default_factory=list,
        description="Values for the template's {{placeholders}}, in order",
    )


class SendResult(BaseModel):
    sent: bool
    message_id: str
    channel: str
    used_template: bool
    window_open: bool


class WindowState(BaseModel):
    window_open: bool
    last_inbound_at: str | None
    can_send_free_form: bool
    explanation: str


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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Connect WhatsApp first"
        )
    return connection


async def _last_inbound(
    business_id: uuid.UUID, phone: str, db: DbDep
) -> tuple[Customer | None, datetime | None]:
    """When this person last wrote to us - which is what opens the window."""
    identity = await db.execute(
        select(CustomerIdentity).where(
            CustomerIdentity.business_id == business_id,
            CustomerIdentity.kind == IdentityKind.phone,
            CustomerIdentity.value == phone,
        )
    )
    found = identity.scalars().first()
    if found is None:
        return None, None

    customer = await db.get(Customer, found.customer_id)
    last = await db.execute(
        select(Message.occurred_at)
        .where(
            Message.customer_id == found.customer_id,
            Message.direction == Direction.inbound,
        )
        .order_by(Message.occurred_at.desc())
        .limit(1)
    )
    return customer, last.scalars().first()


@router.get("/window/{phone}", response_model=WindowState)
async def check_window(
    phone: str, current_user: CurrentUserDep, db: DbDep
) -> WindowState:
    """
    Whether a free-form message to this person will deliver.

    Worth checking before composing rather than after sending: outside the
    window the message is refused, not queued.
    """
    try:
        normalised = normalise_phone(phone)
    except InvalidIdentifier as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _, last_inbound = await _last_inbound(current_user.business, normalised, db)
    open_now = within_service_window(last_inbound)

    if open_now:
        explanation = "They messaged you recently, so you can write anything."
    elif last_inbound is None:
        explanation = (
            "This person has never messaged you. WhatsApp only allows an "
            "approved template to start a conversation."
        )
    else:
        explanation = (
            "More than 24 hours since they last wrote. Only an approved "
            "template will reach them now."
        )

    return WindowState(
        window_open=open_now,
        last_inbound_at=last_inbound.isoformat() if last_inbound else None,
        can_send_free_form=open_now,
        explanation=explanation,
    )


@router.post("/text", response_model=SendResult)
async def send_text(
    body: SendText, current_user: CurrentUserDep, db: DbDep
) -> SendResult:
    """
    Send a free-form message.

    Refused before reaching Meta if the window is closed - a round trip to be
    told no helps nobody, and the error we would surface is less useful than
    the one we can give here.
    """
    try:
        to = normalise_phone(body.to)
    except InvalidIdentifier as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    connection = await _connection(current_user.business, db)
    _, last_inbound = await _last_inbound(current_user.business, to, db)

    if not within_service_window(last_inbound):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The 24-hour window has closed. Use an approved template to "
                "reach this person."
            ),
        )

    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)
    try:
        result = await client.send_text(to, body.body)
    except WhatsAppError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

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
        enqueue_analysis=False,
        db=db,
    )

    return SendResult(
        sent=True,
        message_id=result.external_id,
        channel="whatsapp",
        used_template=False,
        window_open=True,
    )


@router.post("/template", response_model=SendResult)
async def send_template(
    body: SendTemplate, current_user: CurrentUserDep, db: DbDep
) -> SendResult:
    """
    Send using an approved template.

    Works whether or not the window is open - which is the whole point of
    templates, and the only way to start a conversation.
    """
    try:
        to = normalise_phone(body.to)
    except InvalidIdentifier as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    connection = await _connection(current_user.business, db)

    result = await db.execute(
        select(MessageTemplate).where(
            MessageTemplate.business_id == current_user.business,
            MessageTemplate.name == body.template_name,
            MessageTemplate.language == body.language,
        )
    )
    template = result.scalars().first()

    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No template called '{body.template_name}' in {body.language}",
        )
    if template.status != TemplateStatus.approved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"That template is {str(template.status.value).lower()}. Only "
                "approved templates can be sent."
            ),
        )

    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)
    try:
        sent = await client.send_template(
            to, body.template_name, body.language, body_params=body.variables or None
        )
    except WhatsAppError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Store what the customer will actually read, with the variables filled in,
    # rather than the raw template - otherwise the timeline shows {{1}} to the
    # business owner and to the extractor.
    rendered = template.body_text or body.template_name
    for index, value in enumerate(body.variables, start=1):
        rendered = rendered.replace(f"{{{{{index}}}}}", value)

    _, last_inbound = await _last_inbound(current_user.business, to, db)

    await ingest.ingest(
        business_id=current_user.business,
        channel=Channel.whatsapp,
        direction=Direction.outbound,
        identity_kind=IdentityKind.phone,
        identity_value=to,
        external_id=sent.external_id,
        text=rendered,
        occurred_at=datetime.now(timezone.utc),
        connection_id=connection.id,
        raw={"template": body.template_name, "variables": body.variables},
        enqueue_analysis=False,
        db=db,
    )

    logger.info(
        "template sent business=%s template=%s to=%s",
        current_user.business,
        body.template_name,
        to[:6] + "…",
    )

    return SendResult(
        sent=True,
        message_id=sent.external_id,
        channel="whatsapp",
        used_template=True,
        window_open=within_service_window(last_inbound),
    )
