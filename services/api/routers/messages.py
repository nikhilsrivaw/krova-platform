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
    CarouselSendCard,
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


class SendCarouselCard(BaseModel):
    media_id: str
    variables: list[str] = Field(default_factory=list)


class SendTemplate(BaseModel):
    to: str
    template_name: str
    language: str = "en"
    variables: list[str] = Field(
        default_factory=list,
        description="Values for the template's {{placeholders}}, in order",
    )
    # Present only when template_name is a carousel template - one entry per
    # card, in the same order the template was approved with.
    carousel_cards: list[SendCarouselCard] = Field(default_factory=list)


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
        sent_by_user_id=current_user.id,
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
            to, body.template_name, body.language,
            body_params=body.variables or None,
            carousel_cards=[
                CarouselSendCard(media_id=c.media_id, body_params=c.variables)
                for c in body.carousel_cards
            ] or None,
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
        sent_by_user_id=current_user.id,
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


# ── Interactive and catalog sends ────────────────────────────────────────
#
# Same 24-hour-window rule as send_text - Meta does not allow any of these
# as a template component, so there is nothing to check beyond the window.


async def _open_connection_and_window(to_raw: str, current_user: CurrentUserDep, db: DbDep):
    try:
        to = normalise_phone(to_raw)
    except InvalidIdentifier as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    connection = await _connection(current_user.business, db)
    _, last_inbound = await _last_inbound(current_user.business, to, db)
    if not within_service_window(last_inbound):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The 24-hour window has closed. Use an approved template to reach this person.",
        )
    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)
    return to, connection, client


async def _record_outbound(
    current_user: CurrentUserDep, db: DbDep, connection: ChannelConnection,
    to: str, text: str, external_id: str, media: dict, raw: dict,
) -> None:
    await ingest.ingest(
        business_id=current_user.business,
        channel=Channel.whatsapp,
        direction=Direction.outbound,
        identity_kind=IdentityKind.phone,
        identity_value=to,
        external_id=external_id,
        text=text,
        occurred_at=datetime.now(timezone.utc),
        connection_id=connection.id,
        media=media,
        raw=raw,
        enqueue_analysis=False,
        sent_by_user_id=current_user.id,
        db=db,
    )


class ButtonOption(BaseModel):
    id: str = Field(max_length=256)
    title: str = Field(max_length=20)


class SendButtons(BaseModel):
    to: str
    body: str = Field(min_length=1, max_length=1024)
    buttons: list[ButtonOption] = Field(min_length=1, max_length=3)


@router.post("/interactive-buttons", response_model=SendResult)
async def send_interactive_buttons(
    body: SendButtons, current_user: CurrentUserDep, db: DbDep
) -> SendResult:
    """Send up to 3 tappable reply buttons instead of free text to parse back."""
    to, connection, client = await _open_connection_and_window(body.to, current_user, db)
    try:
        result = await client.send_interactive_buttons(
            to, body.body, [(b.id, b.title) for b in body.buttons]
        )
    except (WhatsAppError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    await _record_outbound(
        current_user, db, connection, to, body.body, result.external_id,
        media={"kind": "interactive_buttons", "buttons": [b.model_dump() for b in body.buttons]},
        raw={"buttons": [b.model_dump() for b in body.buttons]},
    )
    return SendResult(sent=True, message_id=result.external_id, channel="whatsapp", used_template=False, window_open=True)


class ListRow(BaseModel):
    id: str
    title: str = Field(max_length=24)
    description: str | None = None


class ListSection(BaseModel):
    title: str
    rows: list[ListRow] = Field(min_length=1)


class SendList(BaseModel):
    to: str
    body: str = Field(min_length=1, max_length=1024)
    button_label: str = Field(max_length=20)
    sections: list[ListSection] = Field(min_length=1)


@router.post("/interactive-list", response_model=SendResult)
async def send_interactive_list(
    body: SendList, current_user: CurrentUserDep, db: DbDep
) -> SendResult:
    """Send a tappable picker - up to 10 rows total across named sections."""
    to, connection, client = await _open_connection_and_window(body.to, current_user, db)
    sections = [
        (s.title, [(r.id, r.title, r.description) for r in s.rows]) for s in body.sections
    ]
    try:
        result = await client.send_interactive_list(to, body.body, body.button_label, sections)
    except (WhatsAppError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    await _record_outbound(
        current_user, db, connection, to, body.body, result.external_id,
        media={"kind": "interactive_list", "sections": [s.model_dump() for s in body.sections]},
        raw={"sections": [s.model_dump() for s in body.sections]},
    )
    return SendResult(sent=True, message_id=result.external_id, channel="whatsapp", used_template=False, window_open=True)


class SendProduct(BaseModel):
    to: str
    catalog_id: str
    product_retailer_id: str
    body: str | None = Field(default=None, max_length=1024)


@router.post("/product", response_model=SendResult)
async def send_product(body: SendProduct, current_user: CurrentUserDep, db: DbDep) -> SendResult:
    """Show one product with its real price and image, from the business's own catalog."""
    to, connection, client = await _open_connection_and_window(body.to, current_user, db)
    try:
        result = await client.send_single_product_message(
            to, body.catalog_id, body.product_retailer_id, body=body.body
        )
    except WhatsAppError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    await _record_outbound(
        current_user, db, connection, to, body.body or "Sent a product", result.external_id,
        media={"kind": "product", "catalog_id": body.catalog_id, "product_retailer_id": body.product_retailer_id},
        raw={"catalog_id": body.catalog_id, "product_retailer_id": body.product_retailer_id},
    )
    return SendResult(sent=True, message_id=result.external_id, channel="whatsapp", used_template=False, window_open=True)


class ProductSection(BaseModel):
    title: str
    product_retailer_ids: list[str] = Field(min_length=1)


class SendProducts(BaseModel):
    to: str
    catalog_id: str
    header: str = Field(max_length=60)
    body: str = Field(min_length=1, max_length=1024)
    sections: list[ProductSection] = Field(min_length=1)


@router.post("/products", response_model=SendResult)
async def send_products(body: SendProducts, current_user: CurrentUserDep, db: DbDep) -> SendResult:
    """Show several products at once, grouped into named sections - up to 30 total."""
    to, connection, client = await _open_connection_and_window(body.to, current_user, db)
    sections = [(s.title, s.product_retailer_ids) for s in body.sections]
    try:
        result = await client.send_multi_product_message(to, body.catalog_id, body.header, body.body, sections)
    except (WhatsAppError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    await _record_outbound(
        current_user, db, connection, to, body.body, result.external_id,
        media={"kind": "product_list", "catalog_id": body.catalog_id, "sections": [s.model_dump() for s in body.sections]},
        raw={"catalog_id": body.catalog_id, "sections": [s.model_dump() for s in body.sections]},
    )
    return SendResult(sent=True, message_id=result.external_id, channel="whatsapp", used_template=False, window_open=True)


class SendCatalog(BaseModel):
    to: str
    body: str = Field(min_length=1, max_length=1024)


@router.post("/catalog", response_model=SendResult)
async def send_catalog(body: SendCatalog, current_user: CurrentUserDep, db: DbDep) -> SendResult:
    """Show the business's whole catalog as a browsable entry point."""
    to, connection, client = await _open_connection_and_window(body.to, current_user, db)
    try:
        result = await client.send_catalog_message(to, body.body)
    except WhatsAppError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    await _record_outbound(
        current_user, db, connection, to, body.body, result.external_id,
        media={"kind": "catalog_message"}, raw={},
    )
    return SendResult(sent=True, message_id=result.external_id, channel="whatsapp", used_template=False, window_open=True)
