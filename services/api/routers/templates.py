"""
Template management for a client's WhatsApp Business Account.

The capability Meta requires of a Tech Provider, and the one that decides
whether a business can start a conversation at all: outside the 24-hour
window, only an approved template delivers.

Approval is asynchronous. Creating a template returns PENDING and nothing
more - the verdict arrives on the message_template_status_update webhook up
to 24 hours later. So this router never waits for it, and the local row is a
mirror the webhook keeps in step.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.ai import carousel_draft
from shared.auth.encryption import decrypt
from shared.channels.whatsapp import media_upload
from shared.channels.whatsapp import templates as meta
from shared.db.models import (
    Business,
    BusinessDNA,
    Channel,
    ChannelConnection,
    ConnectionStatus,
    MessageTemplate,
    TemplateCategory,
    TemplateStatus,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/templates", tags=["templates"])

# Meta's edit ceiling on approved templates: 10 per 30 days, 1 per 24 hours.
EDIT_WINDOW = timedelta(days=30)
MAX_EDITS_PER_WINDOW = 10


class ButtonIn(BaseModel):
    type: Literal["QUICK_REPLY", "URL", "PHONE_NUMBER"]
    text: str = Field(max_length=25)
    url: str | None = None
    phone_number: str | None = None


class CarouselCardIn(BaseModel):
    header_handle: str = Field(min_length=1)
    # Not sent to Meta on create - Meta only wants header_handle for review.
    # Stashed on the template's `extra` so a campaign built against this
    # template later knows which live media to send, without asking someone
    # to re-upload or remember a raw Meta id.
    media_id: str = Field(min_length=1)
    body: str = Field(min_length=1, max_length=160)
    buttons: list[ButtonIn] = Field(default_factory=list, max_length=2)
    examples: dict[str, str] = Field(default_factory=dict)


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    category: Literal["UTILITY", "MARKETING", "AUTHENTICATION"]
    body: str = Field(min_length=1, max_length=1024)
    language: str = Field(default="en", max_length=16)
    header_text: str | None = Field(default=None, max_length=60)
    footer: str | None = Field(default=None, max_length=60)
    buttons: list[ButtonIn] = Field(default_factory=list)
    examples: dict[str, str] = Field(default_factory=dict)
    # 2-10 cards makes this a carousel template instead of a normal one -
    # header_text/footer/buttons above are ignored when this is set, since
    # a carousel puts all of that on each card.
    carousel_cards: list[CarouselCardIn] = Field(default_factory=list, max_length=10)


class TemplateOut(BaseModel):
    id: str
    external_id: str | None
    name: str
    language: str
    category: str
    status: str
    body_text: str | None
    components: list | dict
    rejection_reason: str | None
    sendable: bool
    variables: list[str]
    submitted_at: str | None
    reviewed_at: str | None
    edits_remaining: int | None
    is_carousel: bool
    card_count: int
    carousel_media_ids: list[str]


def _out(t: MessageTemplate) -> TemplateOut:
    status_value = t.status.value if hasattr(t.status, "value") else str(t.status)
    components = t.components if isinstance(t.components, list) else []
    carousel = next((c for c in components if c.get("type") == "CAROUSEL"), None)
    card_count = len(carousel.get("cards", [])) if carousel else 0
    remaining: int | None = None
    if status_value == TemplateStatus.approved.value:
        recent = (
            t.edit_count
            if t.last_edited_at
            and t.last_edited_at > datetime.now(timezone.utc) - EDIT_WINDOW
            else 0
        )
        remaining = max(0, MAX_EDITS_PER_WINDOW - recent)

    return TemplateOut(
        id=str(t.id),
        external_id=t.external_id,
        name=t.name,
        language=t.language,
        category=t.category.value if hasattr(t.category, "value") else str(t.category),
        status=status_value,
        body_text=t.body_text,
        components=components,
        rejection_reason=t.rejection_reason,
        sendable=status_value == TemplateStatus.approved.value,
        variables=meta.variables_in(t.body_text or ""),
        submitted_at=t.submitted_at.isoformat() if t.submitted_at else None,
        reviewed_at=t.reviewed_at.isoformat() if t.reviewed_at else None,
        edits_remaining=remaining,
        is_carousel=carousel is not None,
        card_count=card_count,
        carousel_media_ids=list((t.extra or {}).get("carousel_media_ids") or []),
    )


async def _whatsapp(business_id: uuid.UUID, db: DbDep) -> tuple[ChannelConnection, str]:
    """The client's active WhatsApp connection, and its WABA id."""
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
            status_code=status.HTTP_409_CONFLICT,
            detail="Connect WhatsApp before creating templates",
        )
    waba_id = (connection.extra or {}).get("waba_id")
    if not waba_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This WhatsApp connection is incomplete. Please reconnect.",
        )
    return connection, waba_id


def _draft(body: TemplateIn) -> meta.TemplateDraft:
    return meta.TemplateDraft(
        name=body.name,
        category=body.category,
        body=body.body,
        language=body.language,
        header_text=body.header_text,
        footer=body.footer,
        buttons=[
            meta.Button(type=b.type, text=b.text, url=b.url, phone_number=b.phone_number)
            for b in body.buttons
        ],
        examples=body.examples,
        carousel_cards=[
            meta.CarouselCard(
                header_handle=c.header_handle,
                body=c.body,
                buttons=[
                    meta.Button(type=b.type, text=b.text, url=b.url, phone_number=b.phone_number)
                    for b in c.buttons
                ],
                examples=c.examples,
            )
            for c in body.carousel_cards
        ],
    )


class CarouselImageOut(BaseModel):
    header_handle: str
    media_id: str


@router.post("/carousel/image", response_model=CarouselImageOut)
async def upload_carousel_image(
    current_user: CurrentUserDep, db: DbDep, file: UploadFile = File(...),
) -> CarouselImageOut:
    """
    Upload one card's picture, once, to both of Meta's endpoints - the
    handle a reviewer needs to see it and the media_id an approved send
    will later use. See media_upload.py for why both exist.
    """
    connection, _ = await _whatsapp(current_user.business, db)
    raw = await file.read()

    try:
        uploaded = await media_upload.upload_for_carousel_card(
            raw,
            file.content_type or "",
            file.filename or "card.jpg",
            access_token=decrypt(connection.access_token),
            phone_number_id=connection.external_account_id,
        )
    except media_upload.UploadError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return CarouselImageOut(header_handle=uploaded.header_handle, media_id=uploaded.media_id)


class CarouselDraftIn(BaseModel):
    brief: str = Field(min_length=1, max_length=500)
    card_count: int = Field(default=4, ge=2, le=10)


class CarouselDraftCardOut(BaseModel):
    body: str
    button_label: str


class CarouselDraftOut(BaseModel):
    cards: list[CarouselDraftCardOut]


@router.post("/carousel/draft", response_model=CarouselDraftOut)
async def draft_carousel_cards(
    body: CarouselDraftIn, current_user: CurrentUserDep, db: DbDep,
) -> CarouselDraftOut:
    """
    A starting point for the cards' text, from a one-line brief. Nothing
    here is submitted to Meta - it fills the builder for a human to edit.
    """
    business = await db.get(Business, current_user.business)
    dna = await db.get(BusinessDNA, current_user.business)
    context_parts = [f"{business.name} ({business.vertical})" if business else "A business"]
    if dna and dna.summary:
        context_parts.append(dna.summary)

    result = await carousel_draft.draft(
        brief=body.brief, card_count=body.card_count, business_context="\n".join(context_parts),
    )
    if not result.cards:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not draft cards right now - try again, or write them by hand.",
        )
    return CarouselDraftOut(
        cards=[CarouselDraftCardOut(body=c.body, button_label=c.button_label) for c in result.cards]
    )


@router.get("", response_model=list[TemplateOut])
async def list_templates(
    current_user: CurrentUserDep,
    db: DbDep,
    status_filter: str | None = Query(default=None, alias="status"),
) -> list[TemplateOut]:
    """Templates this business has, newest first."""
    conditions = [MessageTemplate.business_id == current_user.business]
    if status_filter:
        conditions.append(MessageTemplate.status == status_filter.upper())

    result = await db.execute(
        select(MessageTemplate)
        .where(*conditions)
        .order_by(MessageTemplate.created_at.desc())
    )
    return [_out(t) for t in result.scalars().all()]


@router.post("", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(
    body: TemplateIn, current_user: CurrentUserDep, db: DbDep
) -> TemplateOut:
    """
    Submit a template to Meta for review.

    Returns immediately with PENDING. Meta takes up to 24 hours and reports
    the outcome on the webhook.
    """
    connection, waba_id = await _whatsapp(current_user.business, db)
    draft = _draft(body)

    try:
        name = meta.normalise_name(body.name)
        result = await meta.TemplateClient(
            decrypt(connection.access_token), waba_id
        ).create(draft)
    except meta.TemplateError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    now = datetime.now(timezone.utc)
    template = MessageTemplate(
        business_id=current_user.business,
        connection_id=connection.id,
        external_id=result.get("id"),
        name=name,
        language=body.language,
        category=TemplateCategory(body.category),
        # Meta returns its own status; anything other than APPROVED starts
        # as pending review.
        status=(
            TemplateStatus.approved
            if result.get("status") == "APPROVED"
            else TemplateStatus.pending
        ),
        components=draft.to_components(),
        body_text=body.body,
        submitted_at=now,
        extra=(
            {"carousel_media_ids": [c.media_id for c in body.carousel_cards]}
            if body.carousel_cards else {}
        ),
    )
    db.add(template)
    await db.flush()
    return _out(template)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: uuid.UUID,
    current_user: CurrentUserDep,
    db: DbDep,
    all_languages: bool = Query(
        default=False,
        description=(
            "Delete every language variant sharing this name. Meta's default "
            "when no id is given, so it is opt-in here."
        ),
    ),
) -> None:
    template = await db.get(MessageTemplate, template_id)
    if template is None or template.business_id != current_user.business:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Template not found"
        )

    if template.external_id:
        connection, waba_id = await _whatsapp(current_user.business, db)
        try:
            await meta.TemplateClient(
                decrypt(connection.access_token), waba_id
            ).delete(
                template.name,
                template_id=None if all_languages else template.external_id,
            )
        except meta.TemplateError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

    await db.delete(template)


@router.post("/sync", response_model=list[TemplateOut])
async def sync_templates(current_user: CurrentUserDep, db: DbDep) -> list[TemplateOut]:
    """
    Pull the truth from Meta.

    Templates can be created, edited or deleted in WhatsApp Manager without us
    hearing about it, and a webhook can be missed while a server is down. This
    reconciles rather than assuming our mirror is right.
    """
    connection, waba_id = await _whatsapp(current_user.business, db)

    try:
        remote = await meta.TemplateClient(
            decrypt(connection.access_token), waba_id
        ).list()
    except meta.TemplateError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    existing = {
        (t.name, t.language): t
        for t in (
            await db.execute(
                select(MessageTemplate).where(
                    MessageTemplate.business_id == current_user.business
                )
            )
        )
        .scalars()
        .all()
    }

    now = datetime.now(timezone.utc)
    for item in remote:
        key = (item.get("name"), item.get("language"))
        components = item.get("components") or []
        body_text = next(
            (c.get("text") for c in components if c.get("type") == "BODY"), None
        )
        raw_status = (item.get("status") or "").upper()
        mapped = (
            TemplateStatus(raw_status)
            if raw_status in TemplateStatus._value2member_map_
            else TemplateStatus.pending
        )

        template = existing.get(key)
        if template is None:
            template = MessageTemplate(
                business_id=current_user.business,
                connection_id=connection.id,
                name=item.get("name"),
                language=item.get("language") or "en",
                category=TemplateCategory(item.get("category", "UTILITY")),
                submitted_at=now,
            )
            db.add(template)

        template.external_id = item.get("id")
        template.status = mapped
        template.components = components
        template.body_text = body_text
        template.rejection_reason = item.get("rejected_reason") or None
        if mapped in (TemplateStatus.approved, TemplateStatus.rejected):
            template.reviewed_at = template.reviewed_at or now

    await db.flush()

    result = await db.execute(
        select(MessageTemplate)
        .where(MessageTemplate.business_id == current_user.business)
        .order_by(MessageTemplate.created_at.desc())
    )
    return [_out(t) for t in result.scalars().all()]
