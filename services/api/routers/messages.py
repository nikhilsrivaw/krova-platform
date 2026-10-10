"""
Sending messages.

The 24-hour rule decides everything here. Inside the customer service window
a business can write freely; outside it, only an approved template delivers
and a free-form send is refused with error 131047.

So this router does not make the caller remember which situation they are in.
It looks at when the customer last wrote, picks the only thing that can work,
and says plainly when nothing can.
"""

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from services.api.dependencies import CurrentUserDep, DbDep
from shared.auth.encryption import decrypt
from shared.channels import ingest
from shared.channels.whatsapp.carousel_media import instagram_carousel_needs_values
from shared.channels.instagram.client import (
    GenericTemplateButton,
    GenericTemplateElement,
    InstagramApiError,
    InstagramClient,
    InstagramSendError,
)
from shared.integrations import media_storage
from shared.integrations.media_storage import MediaStorageError
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
    InstagramCarousel,
    Message,
    MessageTemplate,
    TemplateStatus,
)
from shared.identity.normalise import InvalidIdentifier, normalise_phone
from shared.audit import activity
from shared.team import conflict
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


async def _guard(current_user, db, *, phone: str | None = None, igsid: str | None = None) -> None:
    """
    Stop an agent replying to a teammate's customer (409 with the owner's name,
    which the apps turn into "Take over"), and give an unowned chat to the first
    agent who answers it. See shared/team/conflict.py.
    """
    from shared.billing.guards import require_active_plan

    await require_active_plan(db, current_user.business)
    try:
        await conflict.guard_reply_to(
            db, business_id=current_user.business,
            kind="phone" if phone else "instagram", value=phone or igsid or "",
            actor_id=current_user.id, actor_role=current_user.role,
        )
    except conflict.AssignedToOther as exc:
        raise conflict.to_http(exc) from exc


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
        activity.note(to=activity.mask_phone(to), channel="whatsapp")
    except InvalidIdentifier as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _guard(current_user, db, phone=to)

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
        activity.note(to=activity.mask_phone(to), channel="whatsapp")
    except InvalidIdentifier as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _guard(current_user, db, phone=to)

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
    await _guard(current_user, db, phone=to)

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


# ── Instagram ────────────────────────────────────────────────────────────
#
# One send endpoint, no window check - Meta enforces the 24-hour window
# server-side and rejects the call if it's closed, and there is no local way
# to check it ourselves yet (see client.py's docstring). `to` is the
# recipient's Instagram-scoped id (IGSID), not a username - Meta's Send API
# does not accept usernames.


class SendInstagramText(BaseModel):
    to: str = Field(description="Recipient's Instagram-scoped id (IGSID)")
    body: str = Field(min_length=1, max_length=1000)


class InstagramParticipantOut(BaseModel):
    id: str
    username: str | None


class InstagramConversationOut(BaseModel):
    id: str
    participants: list[InstagramParticipantOut]


async def _active_instagram_connection(business_id, db) -> ChannelConnection:
    result = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == business_id,
            ChannelConnection.channel == Channel.instagram,
            ChannelConnection.status == ConnectionStatus.active,
        )
    )
    connection = result.scalars().first()
    if connection is None or not connection.access_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Connect Instagram first"
        )
    return connection


@router.get("/instagram/conversations", response_model=list[InstagramConversationOut])
async def list_instagram_conversations(
    current_user: CurrentUserDep, db: DbDep
) -> list[InstagramConversationOut]:
    """
    Who this business can message on Instagram right now, with the IGSID
    each one is addressed by - read live from Meta rather than from our own
    messages table, because a conversation Meta knows about is exactly the
    set the Send API will accept, and the two can differ.
    """
    connection = await _active_instagram_connection(current_user.business, db)
    client = InstagramClient.for_connection(connection)
    try:
        conversations = await client.list_conversations()
    except InstagramApiError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return [
        InstagramConversationOut(
            id=c.id,
            participants=[
                InstagramParticipantOut(id=p.id, username=p.username) for p in c.participants
            ],
        )
        for c in conversations
    ]


async def _last_inbound_instagram(
    business_id: uuid.UUID, igsid: str, db: DbDep
) -> datetime | None:
    """When this Instagram person last wrote to us - the phone/_last_inbound
    helper's own logic, addressed by IGSID instead."""
    identity = await db.execute(
        select(CustomerIdentity).where(
            CustomerIdentity.business_id == business_id,
            CustomerIdentity.kind == IdentityKind.instagram,
            CustomerIdentity.value == igsid,
        )
    )
    found = identity.scalars().first()
    if found is None:
        return None
    last = await db.execute(
        select(Message.occurred_at)
        .where(
            Message.customer_id == found.customer_id,
            Message.direction == Direction.inbound,
            Message.channel == Channel.instagram,
        )
        .order_by(Message.occurred_at.desc())
        .limit(1)
    )
    return last.scalar_one_or_none()


class InstagramWindowState(BaseModel):
    can_send_free_form: bool
    # True only when can_send_free_form is - whether sending now requires
    # (and will automatically use) the HUMAN_AGENT tag, so the UI can tell
    # a staff member which situation they're in rather than just a flat
    # yes/no the way WhatsApp's WindowState is.
    requires_human_agent_tag: bool
    last_inbound_at: str | None
    explanation: str


@router.get("/instagram/window/{igsid}", response_model=InstagramWindowState)
async def instagram_window_state(
    igsid: str, current_user: CurrentUserDep, db: DbDep
) -> InstagramWindowState:
    last_inbound = await _last_inbound_instagram(current_user.business, igsid, db)
    if last_inbound is None:
        return InstagramWindowState(
            can_send_free_form=False, requires_human_agent_tag=False, last_inbound_at=None,
            explanation="This person has never messaged you. Instagram requires them to message first.",
        )
    age = datetime.now(timezone.utc) - last_inbound
    if age <= timedelta(hours=24):
        return InstagramWindowState(
            can_send_free_form=True, requires_human_agent_tag=False,
            last_inbound_at=last_inbound.isoformat(),
            explanation="They messaged you recently, so you can write anything.",
        )
    if age <= timedelta(days=7):
        return InstagramWindowState(
            can_send_free_form=True, requires_human_agent_tag=True,
            last_inbound_at=last_inbound.isoformat(),
            explanation=(
                "More than 24 hours since they last wrote. You can still reply "
                "as a human agent (sent as you, not automated) for up to 7 days "
                "total."
            ),
        )
    return InstagramWindowState(
        can_send_free_form=False, requires_human_agent_tag=False,
        last_inbound_at=last_inbound.isoformat(),
        explanation="It's been over 7 days since they last messaged - Instagram no longer allows a reply.",
    )


@router.post("/instagram/text", response_model=SendResult)
async def send_instagram_text(
    body: SendInstagramText, current_user: CurrentUserDep, db: DbDep
) -> SendResult:
    """
    A staff member's own manually-typed reply - never an AI-drafted or
    automated send, so (unlike every other Instagram send in this file)
    this is exactly the case Meta's HUMAN_AGENT tag exists for: a real
    person continuing a conversation. Used automatically, not left to the
    caller, since every call into this endpoint already is one.
    """
    await _guard(current_user, db, igsid=body.to)
    last_inbound = await _last_inbound_instagram(current_user.business, body.to, db)
    if last_inbound is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This person has never messaged you - Instagram requires them to "
            "message first before you can reply.",
        )
    age = datetime.now(timezone.utc) - last_inbound
    if age > timedelta(days=7):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "It's been over 7 days since they last messaged - Instagram no "
            "longer allows a reply, even as a human agent. They need to "
            "message again first.",
        )
    use_human_agent_tag = age > timedelta(hours=24)

    connection = await _active_instagram_connection(current_user.business, db)
    client = InstagramClient.for_connection(connection)
    try:
        sent = await client.send_text(body.to, body.body, human_agent=use_human_agent_tag)
    except InstagramSendError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    await ingest.ingest(
        business_id=current_user.business,
        channel=Channel.instagram,
        direction=Direction.outbound,
        identity_kind=IdentityKind.instagram,
        identity_value=body.to,
        external_id=sent.external_id or None,
        text=body.body,
        occurred_at=datetime.now(timezone.utc),
        connection_id=connection.id,
        enqueue_analysis=False,
        sent_by_user_id=current_user.id,
        db=db,
    )

    return SendResult(
        sent=True,
        message_id=sent.external_id,
        channel="instagram",
        used_template=False,
        window_open=True,
    )


class InstagramCarouselImageOut(BaseModel):
    image_url: str


@router.post("/instagram/carousel/image", response_model=InstagramCarouselImageOut)
async def upload_instagram_carousel_image(
    current_user: CurrentUserDep, db: DbDep, file: UploadFile = File(...),
) -> InstagramCarouselImageOut:
    """
    Host one carousel card's picture at a public URL - Instagram's generic
    template takes image_url directly (Meta fetches it itself), the same
    contract publish_instagram_photo below already uses, so this reuses
    the same bucket rather than a second upload path.
    """
    await _active_instagram_connection(current_user.business, db)
    content = await file.read()
    content_type = file.content_type or "application/octet-stream"
    try:
        image_url = await media_storage.upload_media(content, content_type)
    except MediaStorageError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return InstagramCarouselImageOut(image_url=image_url)


class InstagramCarouselButtonIn(BaseModel):
    type: Literal["web_url", "postback"]
    title: str = Field(min_length=1, max_length=20)
    url: str | None = None
    payload: str | None = None


class InstagramCarouselElementIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    subtitle: str | None = Field(default=None, max_length=80)
    image_url: str | None = None
    buttons: list[InstagramCarouselButtonIn] = Field(default_factory=list, max_length=3)


class SendInstagramCarousel(BaseModel):
    to: str = Field(description="Recipient's Instagram-scoped id (IGSID)")
    elements: list[InstagramCarouselElementIn] = Field(min_length=1, max_length=10)


@router.post("/instagram/carousel", response_model=SendResult)
async def send_instagram_carousel(
    body: SendInstagramCarousel, current_user: CurrentUserDep, db: DbDep
) -> SendResult:
    """
    Instagram's "Generic Template" - a horizontally-scrollable carousel.
    No Meta review needed (unlike a WhatsApp template): sends instantly,
    subject only to the same 24-hour window Meta enforces for every
    Instagram message.
    """
    for element in body.elements:
        for button in element.buttons:
            if button.type == "web_url" and not button.url:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Button '{button.title}' is a link but has no URL",
                )
            if button.type == "postback" and not button.payload:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Button '{button.title}' needs a payload",
                )

    await _guard(current_user, db, igsid=body.to)
    connection = await _active_instagram_connection(current_user.business, db)
    client = InstagramClient.for_connection(connection)
    try:
        sent = await client.send_generic_template(
            body.to,
            [
                GenericTemplateElement(
                    title=e.title,
                    subtitle=e.subtitle,
                    image_url=e.image_url,
                    buttons=[
                        GenericTemplateButton(
                            type=b.type, title=b.title, url=b.url, payload=b.payload,
                        )
                        for b in e.buttons
                    ],
                )
                for e in body.elements
            ],
        )
    except InstagramSendError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    await ingest.ingest(
        business_id=current_user.business,
        channel=Channel.instagram,
        direction=Direction.outbound,
        identity_kind=IdentityKind.instagram,
        identity_value=body.to,
        external_id=sent.external_id or None,
        text=f"[Carousel: {', '.join(e.title for e in body.elements)}]",
        occurred_at=datetime.now(timezone.utc),
        connection_id=connection.id,
        enqueue_analysis=False,
        sent_by_user_id=current_user.id,
        db=db,
    )

    return SendResult(
        sent=True,
        message_id=sent.external_id,
        channel="instagram",
        used_template=False,
        window_open=True,
    )


_CAROUSEL_NAME_INVALID = re.compile(r"[^a-z0-9_]+")


def _normalise_carousel_name(raw: str) -> str:
    """Lowercase/underscore only - what the agent (share_carousel) and a
    staff member picking from a list both match against exactly."""
    name = _CAROUSEL_NAME_INVALID.sub("_", raw.strip().lower()).strip("_")
    name = re.sub(r"_{2,}", "_", name)
    return name[:100]


class SavedInstagramCarouselOut(BaseModel):
    id: str
    name: str
    description: str
    elements: list


def _saved_carousel_out(c: InstagramCarousel) -> SavedInstagramCarouselOut:
    return SavedInstagramCarouselOut(
        id=str(c.id), name=c.name, description=c.description, elements=c.elements or [],
    )


@router.get("/instagram/carousels", response_model=list[SavedInstagramCarouselOut])
async def list_instagram_carousels(
    current_user: CurrentUserDep, db: DbDep
) -> list[SavedInstagramCarouselOut]:
    """
    Saved carousels this business can offer again - the same list
    shared/ai/context.py shows the agent under "Available Instagram
    carousels" (so it can pick one by name via share_carousel), and what
    a staff member can re-send by hand without rebuilding it card by card.
    """
    rows = await db.execute(
        select(InstagramCarousel)
        .where(InstagramCarousel.business_id == current_user.business)
        .order_by(InstagramCarousel.name)
    )
    return [_saved_carousel_out(c) for c in rows.scalars().all()]


class SaveInstagramCarousel(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=300)
    elements: list[InstagramCarouselElementIn] = Field(min_length=1, max_length=10)


@router.post(
    "/instagram/carousels", response_model=SavedInstagramCarouselOut,
    status_code=status.HTTP_201_CREATED,
)
async def save_instagram_carousel(
    body: SaveInstagramCarousel, current_user: CurrentUserDep, db: DbDep
) -> SavedInstagramCarouselOut:
    """Save a carousel under a name, so the agent or a staff member can send it again without rebuilding it."""
    name = _normalise_carousel_name(body.name)
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Give the carousel a name")

    for element in body.elements:
        for button in element.buttons:
            if button.type == "web_url" and not button.url:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"Button '{button.title}' is a link but has no URL",
                )
            if button.type == "postback" and not button.payload:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"Button '{button.title}' needs a payload",
                )

    carousel = InstagramCarousel(
        business_id=current_user.business,
        name=name,
        description=body.description.strip(),
        elements=[e.model_dump(exclude_none=True) for e in body.elements],
    )
    db.add(carousel)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A carousel named '{name}' already exists",
        ) from exc

    return _saved_carousel_out(carousel)


@router.delete("/instagram/carousels/{carousel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_instagram_carousel(
    carousel_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep
) -> None:
    carousel = await db.get(InstagramCarousel, carousel_id)
    if carousel is None or carousel.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Carousel not found")
    await db.delete(carousel)


class SendSavedInstagramCarousel(BaseModel):
    to: str = Field(description="Recipient's Instagram-scoped id (IGSID)")


@router.post("/instagram/carousels/{carousel_id}/send", response_model=SendResult)
async def send_saved_instagram_carousel(
    carousel_id: uuid.UUID, body: SendSavedInstagramCarousel,
    current_user: CurrentUserDep, db: DbDep,
) -> SendResult:
    """
    A staff member re-sending a saved carousel by hand - same Meta call as
    POST /instagram/carousel, built from stored cards instead of ones just
    typed into the one-off composer.
    """
    carousel = await db.get(InstagramCarousel, carousel_id)
    if carousel is None or carousel.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Carousel not found")
    await _guard(current_user, db, igsid=body.to)
    if instagram_carousel_needs_values(carousel.elements):
        # {{1}}-style blanks are for the AI to fill from a conversation. Sent by
        # hand there is nothing to fill them with, and the customer would
        # receive the literal "{{1}}".
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This carousel has {{placeholders}} that only the AI fills in from a conversation, "
            "so it can't be sent by hand.",
        )

    connection = await _active_instagram_connection(current_user.business, db)
    client = InstagramClient.for_connection(connection)
    elements = [
        GenericTemplateElement(
            title=e.get("title", ""), subtitle=e.get("subtitle"), image_url=e.get("image_url"),
            buttons=[GenericTemplateButton(**b) for b in e.get("buttons", [])],
        )
        for e in (carousel.elements or [])
    ]
    try:
        sent = await client.send_generic_template(body.to, elements)
    except InstagramSendError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    await ingest.ingest(
        business_id=current_user.business,
        channel=Channel.instagram,
        direction=Direction.outbound,
        identity_kind=IdentityKind.instagram,
        identity_value=body.to,
        external_id=sent.external_id or None,
        text=f"[Carousel: {carousel.name}]",
        occurred_at=datetime.now(timezone.utc),
        connection_id=connection.id,
        enqueue_analysis=False,
        sent_by_user_id=current_user.id,
        db=db,
    )

    return SendResult(
        sent=True, message_id=sent.external_id, channel="instagram",
        used_template=False, window_open=True,
    )


class InstagramInsightsOut(BaseModel):
    period_days: int
    values: dict[str, int]


@router.get("/instagram/insights", response_model=InstagramInsightsOut)
async def instagram_insights(
    current_user: CurrentUserDep, db: DbDep, days: int = 7
) -> InstagramInsightsOut:
    """
    Account-level engagement metrics for the connected Instagram account -
    read live from Meta rather than stored, since Krova has no reason to
    keep a second copy of numbers Meta already computes and owns.
    """
    connection = await _active_instagram_connection(current_user.business, db)
    client = InstagramClient.for_connection(connection)
    insights = await client.get_account_insights(period_days=days)
    return InstagramInsightsOut(period_days=insights.period_days, values=insights.values)


class PublishPhotoOut(BaseModel):
    media_id: str
    image_url: str


@router.post("/instagram/publish", response_model=PublishPhotoOut)
async def publish_instagram_photo(
    current_user: CurrentUserDep, db: DbDep,
    file: UploadFile = File(...), caption: str = Form(default=""),
) -> PublishPhotoOut:
    """
    Publish a photo to this business's Instagram feed.

    Two hops, not one: the file goes to Krova's own public bucket first
    (shared/integrations/media_storage.py) because Meta's publish API
    fetches media from a URL itself rather than accepting an upload -
    only then can Instagram's own two-call container/publish flow run
    against a URL it can actually reach.
    """
    connection = await _active_instagram_connection(current_user.business, db)
    content = await file.read()
    content_type = file.content_type or "application/octet-stream"

    try:
        image_url = await media_storage.upload_media(content, content_type)
    except MediaStorageError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    client = InstagramClient.for_connection(connection)
    try:
        result = await client.publish_photo(image_url, caption=caption)
    except InstagramApiError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return PublishPhotoOut(media_id=result.media_id, image_url=image_url)
