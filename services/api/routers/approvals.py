"""
The approvals queue.

Where human-in-the-loop stops being a claim and becomes a screen. Every reply
the agent proposes waits here until a person approves, edits or rejects it.

Three things make this more useful than a simple yes/no:

The reasoning is shown. An owner deciding whether to trust a reply needs to
see why the agent said it and what it read, the same way the ledger cites its
messages.

Edits are kept separately from the original. The difference between what the
agent wrote and what a person actually sent is the clearest signal available
about where it is going wrong.

Rejections ask why, optionally. A rejected draft with a reason is worth more
than ten approvals.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.auth.encryption import decrypt
from shared.channels import ingest
from shared.channels.whatsapp.client import WhatsAppClient, WhatsAppError
from shared.db.models import (
    Business,
    Channel,
    ChannelConnection,
    ConnectionStatus,
    Customer,
    CustomerIdentity,
    Direction,
    DraftStatus,
    IdentityKind,
    Message,
    MessageDraft,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


class DraftOut(BaseModel):
    id: str
    customer_id: str
    customer_name: str | None
    channel: str
    action: str
    status: str
    body: str | None
    reasoning: str | None
    gap: str | None
    confidence: float
    low_confidence: bool
    replying_to: str | None
    expires_at: str | None
    expired: bool
    created_at: str


class ApproveBody(BaseModel):
    # What the person is actually sending. Absent means send the agent's words
    # unchanged.
    body: str | None = Field(default=None, max_length=4096)


class RejectBody(BaseModel):
    note: str | None = Field(default=None, max_length=500)


def _value(v) -> str:
    return v.value if hasattr(v, "value") else str(v)


async def _out(draft: MessageDraft, db: DbDep) -> DraftOut:
    customer = await db.get(Customer, draft.customer_id)
    replying_to = None
    if draft.in_reply_to_id:
        source = await db.get(Message, draft.in_reply_to_id)
        replying_to = (source.content or "")[:300] if source else None

    now = datetime.now(timezone.utc)
    expires = draft.expires_at
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)

    return DraftOut(
        id=str(draft.id),
        customer_id=str(draft.customer_id),
        customer_name=customer.display_name if customer else None,
        channel=draft.channel,
        action=_value(draft.action),
        status=_value(draft.status),
        body=draft.final_body,
        reasoning=draft.reasoning,
        gap=draft.gap,
        confidence=draft.confidence,
        low_confidence=draft.confidence < 0.6,
        replying_to=replying_to,
        expires_at=expires.isoformat() if expires else None,
        expired=bool(expires and expires < now),
        created_at=draft.created_at.isoformat(),
    )


async def _owned(draft_id: uuid.UUID, business_id: uuid.UUID, db: DbDep) -> MessageDraft:
    draft = await db.get(MessageDraft, draft_id)
    if draft is None or draft.business_id != business_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found"
        )
    return draft


@router.get("", response_model=list[DraftOut])
async def list_drafts(
    current_user: CurrentUserDep,
    db: DbDep,
    status_filter: str | None = Query(default="pending", alias="status"),
    limit: int = Query(default=50, le=200),
) -> list[DraftOut]:
    """What the agent is waiting on, oldest first — the window closes."""
    conditions = [MessageDraft.business_id == current_user.business]
    if status_filter and status_filter != "all":
        conditions.append(MessageDraft.status == status_filter)

    result = await db.execute(
        select(MessageDraft)
        .where(*conditions)
        .order_by(MessageDraft.created_at.asc())
        .limit(limit)
    )
    return [await _out(d, db) for d in result.scalars().all()]


@router.get("/count")
async def pending_count(current_user: CurrentUserDep, db: DbDep) -> dict:
    """For the badge on the nav."""
    total = await db.execute(
        select(func.count(MessageDraft.id)).where(
            MessageDraft.business_id == current_user.business,
            MessageDraft.status == DraftStatus.pending,
        )
    )
    escalations = await db.execute(
        select(func.count(MessageDraft.id)).where(
            MessageDraft.business_id == current_user.business,
            MessageDraft.status == DraftStatus.pending,
            MessageDraft.action == "escalate",
        )
    )
    return {
        "pending": int(total.scalar_one()),
        "needs_you": int(escalations.scalar_one()),
    }


@router.post("/{draft_id}/approve", response_model=DraftOut)
async def approve(
    draft_id: uuid.UUID, body: ApproveBody, current_user: CurrentUserDep, db: DbDep
) -> DraftOut:
    """
    Send it.

    An edit is stored alongside the original rather than replacing it - the
    difference between what the agent wrote and what a person sent is the
    best signal we get about where it is wrong.
    """
    draft = await _owned(draft_id, current_user.business, db)

    if draft.status != DraftStatus.pending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This draft is already {_value(draft.status)}",
        )

    now = datetime.now(timezone.utc)
    expires = draft.expires_at
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires and expires < now:
        draft.status = DraftStatus.expired
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The 24-hour window closed before this was approved. Use a "
                "template to reach this customer."
            ),
        )

    if body.body is not None and body.body.strip() != (draft.body or "").strip():
        draft.edited_body = body.body.strip()

    text = draft.final_body
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="There is nothing to send. Write a reply or reject this.",
        )

    phone = await db.execute(
        select(CustomerIdentity.value).where(
            CustomerIdentity.customer_id == draft.customer_id,
            CustomerIdentity.kind == IdentityKind.phone,
        )
    )
    to = phone.scalars().first()
    if not to:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No phone number on file for this customer",
        )

    connection = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == current_user.business,
                ChannelConnection.channel == Channel.whatsapp,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()
    if connection is None or not connection.access_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="WhatsApp is not connected"
        )

    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)
    try:
        sent = await client.send_text(to, text)
    except WhatsAppError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    stored = await ingest.ingest(
        business_id=current_user.business,
        channel=Channel.whatsapp,
        direction=Direction.outbound,
        identity_kind=IdentityKind.phone,
        identity_value=to,
        external_id=sent.external_id,
        text=text,
        occurred_at=now,
        connection_id=connection.id,
        enqueue_analysis=False,
        db=db,
    )

    draft.status = DraftStatus.sent
    draft.reviewed_by_user_id = current_user.id
    draft.reviewed_at = now
    draft.sent_message_id = stored.message.id if stored.message else None

    logger.info(
        "draft approved and sent business=%s draft=%s edited=%s",
        current_user.business,
        draft.id,
        draft.edited_body is not None,
    )
    return await _out(draft, db)


@router.post("/{draft_id}/reject", response_model=DraftOut)
async def reject(
    draft_id: uuid.UUID, body: RejectBody, current_user: CurrentUserDep, db: DbDep
) -> DraftOut:
    """
    Discard it.

    The note is optional but valuable - a rejected draft with a reason says
    more about what is going wrong than ten approvals say about what is right.
    """
    draft = await _owned(draft_id, current_user.business, db)
    if draft.status != DraftStatus.pending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This draft is already {_value(draft.status)}",
        )

    draft.status = DraftStatus.rejected
    draft.rejection_note = (body.note or "").strip() or None
    draft.reviewed_by_user_id = current_user.id
    draft.reviewed_at = datetime.now(timezone.utc)

    logger.info("draft rejected business=%s reason=%r", current_user.business,
                (draft.rejection_note or "")[:80])
    return await _out(draft, db)


class AutonomyBody(BaseModel):
    autonomy: str = Field(pattern="^(observe|draft|act)$")


@router.post("/autonomy")
async def set_autonomy(
    body: AutonomyBody, current_user: CurrentUserDep, db: DbDep
) -> dict:
    """
    How much the agent may do without a person.

    observe  it reads and extracts, but writes nothing
    draft    it proposes replies for you to approve
    act      it replies on its own

    Deliberately a stored, auditable setting rather than a code branch. The
    public promise is human-in-the-loop, so moving off `draft` has to be a
    decision someone made, with a record of when.
    """
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status_code=404, detail="Business not found")

    previous = business.autonomy
    business.autonomy = body.autonomy
    logger.info(
        "autonomy changed business=%s %s -> %s by user=%s",
        business.id,
        previous,
        body.autonomy,
        current_user.id,
    )
    return {"autonomy": body.autonomy, "previous": previous}
