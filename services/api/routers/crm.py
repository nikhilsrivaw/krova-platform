"""
The Krova CRM surface: tags, notes and pipeline stage on top of a customer.

See shared.db.models.crm for the philosophy - a tag is either something the
platform already worked out and a human confirmed, or something a human
wrote because no conversation could have told us. This router is deliberately
thin: the actual proposing happens in shared.crm.tagging, run from the
nightly profile worker, not from a request handler.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import (
    Business,
    Customer,
    CustomerIntelligence,
    CustomerNote,
    CustomerTag,
    TagStatus,
    User,
)

router = APIRouter(prefix="/crm", tags=["crm"])


class TagOut(BaseModel):
    id: str
    label: str
    status: str
    reasoning: str | None
    created_by_user_id: str | None
    decided_by_user_id: str | None
    decided_at: str | None
    created_at: str


class TagIn(BaseModel):
    label: str = Field(min_length=1, max_length=60)


class NoteOut(BaseModel):
    id: str
    body: str
    author_user_id: str | None
    author_name: str | None
    created_at: str


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class StageIn(BaseModel):
    stage: str | None = Field(default=None, max_length=60)


class PipelineStagesOut(BaseModel):
    stages: list[str]


class PipelineStagesIn(BaseModel):
    stages: list[str] = Field(max_length=20)


class PipelineCard(BaseModel):
    customer_id: str
    name: str | None
    deal_value_paise: int | None
    health_score: int | None
    tags: list[str]


class PipelineColumn(BaseModel):
    # None is the "not staged yet" bucket - always present, always first, so
    # a business new to the pipeline sees every customer land somewhere.
    stage: str | None
    total_deal_value_paise: int
    customers: list[PipelineCard]


class PipelineBoard(BaseModel):
    columns: list[PipelineColumn]


class BulkTagIn(BaseModel):
    customer_ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)
    label: str = Field(min_length=1, max_length=60)


class BulkTagResult(BaseModel):
    tagged: int
    already_tagged: int
    not_found: int


def _tag_out(t: CustomerTag) -> TagOut:
    return TagOut(
        id=str(t.id),
        label=t.label,
        status=t.status.value if hasattr(t.status, "value") else str(t.status),
        reasoning=t.reasoning,
        created_by_user_id=str(t.created_by_user_id) if t.created_by_user_id else None,
        decided_by_user_id=str(t.decided_by_user_id) if t.decided_by_user_id else None,
        decided_at=t.decided_at.isoformat() if t.decided_at else None,
        created_at=t.created_at.isoformat(),
    )


async def _owned_customer(customer_id: uuid.UUID, business_id: uuid.UUID, db: DbDep) -> Customer:
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.business_id != business_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return customer


async def _owned_tag(tag_id: uuid.UUID, business_id: uuid.UUID, db: DbDep) -> CustomerTag:
    tag = await db.get(CustomerTag, tag_id)
    if tag is None or tag.business_id != business_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
    return tag


@router.get("/tags", response_model=list[str])
async def list_business_tags(current_user: CurrentUserDep, db: DbDep) -> list[str]:
    """
    Every confirmed label in use across this business's customers - for a
    campaign's "reach everyone tagged X" picker, so it offers real tags
    rather than asking someone to type one from memory.
    """
    rows = await db.execute(
        select(CustomerTag.label)
        .where(CustomerTag.business_id == current_user.business, CustomerTag.status == TagStatus.confirmed)
        .distinct()
        .order_by(CustomerTag.label)
    )
    return [label for (label,) in rows.all()]


@router.get("/customers/{customer_id}/tags", response_model=list[TagOut])
async def list_tags(
    customer_id: uuid.UUID,
    current_user: CurrentUserDep,
    db: DbDep,
    include_rejected: bool = False,
) -> list[TagOut]:
    await _owned_customer(customer_id, current_user.business, db)
    conditions = [CustomerTag.customer_id == customer_id]
    if not include_rejected:
        conditions.append(CustomerTag.status != TagStatus.rejected)
    rows = await db.execute(
        select(CustomerTag).where(*conditions).order_by(CustomerTag.created_at.desc())
    )
    return [_tag_out(t) for t in rows.scalars().all()]


@router.post("/customers/{customer_id}/tags", response_model=TagOut, status_code=status.HTTP_201_CREATED)
async def add_tag(
    customer_id: uuid.UUID, body: TagIn, current_user: CurrentUserDep, db: DbDep
) -> TagOut:
    """A human's own label - confirmed the moment it's written, nothing to review."""
    await _owned_customer(customer_id, current_user.business, db)
    label = body.label.strip().lower()
    if not label:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Label can't be empty")

    existing = (
        await db.execute(
            select(CustomerTag).where(
                CustomerTag.customer_id == customer_id, CustomerTag.label == label,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Re-adding a tag that was previously rejected, or already there -
        # bring it (back) to confirmed rather than erroring or duplicating.
        existing.status = TagStatus.confirmed
        existing.decided_by_user_id = current_user.id
        existing.decided_at = datetime.now(timezone.utc)
        return _tag_out(existing)

    tag = CustomerTag(
        business_id=current_user.business,
        customer_id=customer_id,
        label=label,
        status=TagStatus.confirmed,
        created_by_user_id=current_user.id,
        decided_by_user_id=current_user.id,
        decided_at=datetime.now(timezone.utc),
    )
    db.add(tag)
    await db.flush()
    return _tag_out(tag)


@router.post("/tags/{tag_id}/confirm", response_model=TagOut)
async def confirm_tag(tag_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> TagOut:
    tag = await _owned_tag(tag_id, current_user.business, db)
    tag.status = TagStatus.confirmed
    tag.decided_by_user_id = current_user.id
    tag.decided_at = datetime.now(timezone.utc)
    return _tag_out(tag)


@router.post("/tags/{tag_id}/reject", response_model=TagOut)
async def reject_tag(tag_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> TagOut:
    """
    Say no. The row stays - rejected, not deleted - so the rule that
    proposed this label never proposes it again for this customer.
    """
    tag = await _owned_tag(tag_id, current_user.business, db)
    tag.status = TagStatus.rejected
    tag.decided_by_user_id = current_user.id
    tag.decided_at = datetime.now(timezone.utc)
    return _tag_out(tag)


@router.delete("/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(tag_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
    """
    Remove a tag entirely - unlike reject, this frees the label to be
    suggested or added again later.
    """
    tag = await _owned_tag(tag_id, current_user.business, db)
    await db.delete(tag)


@router.get("/customers/{customer_id}/notes", response_model=list[NoteOut])
async def list_notes(customer_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> list[NoteOut]:
    await _owned_customer(customer_id, current_user.business, db)
    rows = await db.execute(
        select(CustomerNote, User.full_name)
        .outerjoin(User, User.id == CustomerNote.author_user_id)
        .where(CustomerNote.customer_id == customer_id)
        .order_by(CustomerNote.created_at.desc())
    )
    return [
        NoteOut(
            id=str(n.id), body=n.body,
            author_user_id=str(n.author_user_id) if n.author_user_id else None,
            author_name=name, created_at=n.created_at.isoformat(),
        )
        for n, name in rows.all()
    ]


@router.post("/customers/{customer_id}/notes", response_model=NoteOut, status_code=status.HTTP_201_CREATED)
async def add_note(
    customer_id: uuid.UUID, body: NoteIn, current_user: CurrentUserDep, db: DbDep
) -> NoteOut:
    await _owned_customer(customer_id, current_user.business, db)
    note = CustomerNote(
        business_id=current_user.business,
        customer_id=customer_id,
        author_user_id=current_user.id,
        body=body.body.strip(),
    )
    db.add(note)
    await db.flush()
    return NoteOut(
        id=str(note.id), body=note.body, author_user_id=str(current_user.id),
        author_name=current_user.email, created_at=note.created_at.isoformat(),
    )


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(note_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
    note = await db.get(CustomerNote, note_id)
    if note is None or note.business_id != current_user.business:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    await db.delete(note)


@router.patch("/customers/{customer_id}/stage", response_model=dict)
async def set_stage(
    customer_id: uuid.UUID, body: StageIn, current_user: CurrentUserDep, db: DbDep
) -> dict:
    customer = await _owned_customer(customer_id, current_user.business, db)
    customer.stage = body.stage.strip() if body.stage else None
    return {"customer_id": str(customer_id), "stage": customer.stage}


class DealValueIn(BaseModel):
    # None clears it - a lead that turned out to have no realistic value.
    deal_value_paise: int | None = Field(default=None, ge=0)


@router.patch("/customers/{customer_id}/deal-value", response_model=dict)
async def set_deal_value(
    customer_id: uuid.UUID, body: DealValueIn, current_user: CurrentUserDep, db: DbDep
) -> dict:
    customer = await _owned_customer(customer_id, current_user.business, db)
    customer.deal_value_paise = body.deal_value_paise
    return {"customer_id": str(customer_id), "deal_value_paise": customer.deal_value_paise}


DEFAULT_PIPELINE_STAGES = ["New", "Contacted", "Qualified", "Won", "Lost"]


@router.get("/pipeline-stages", response_model=PipelineStagesOut)
async def get_pipeline_stages(current_user: CurrentUserDep, db: DbDep) -> PipelineStagesOut:
    business = await db.get(Business, current_user.business)
    stages = (business.settings or {}).get("pipeline_stages") if business else None
    return PipelineStagesOut(stages=stages if stages else DEFAULT_PIPELINE_STAGES)


@router.put("/pipeline-stages", response_model=PipelineStagesOut)
async def set_pipeline_stages(
    body: PipelineStagesIn, current_user: CurrentUserDep, db: DbDep
) -> PipelineStagesOut:
    """
    A business's own funnel, in its own words - a coaching business's stages
    are not a clinic's. Stored in Business.settings rather than a new table,
    since it is one small list a business rarely touches.
    """
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Business not found")
    cleaned = [s.strip() for s in body.stages if s.strip()][:20]
    business.settings = {**(business.settings or {}), "pipeline_stages": cleaned}
    return PipelineStagesOut(stages=cleaned or DEFAULT_PIPELINE_STAGES)


@router.get("/pipeline", response_model=PipelineBoard)
async def pipeline_board(current_user: CurrentUserDep, db: DbDep) -> PipelineBoard:
    """
    Every customer, grouped into the columns a kanban board draws.

    One purpose-built query rather than making the client re-group a flat
    customer list - the board is the whole point of a pipeline view, so it
    should not depend on doing that arithmetic in the browser correctly.
    """
    business = await db.get(Business, current_user.business)
    stages = (business.settings or {}).get("pipeline_stages") if business else None
    stage_order = stages if stages else DEFAULT_PIPELINE_STAGES

    customers = (
        await db.execute(
            select(Customer).where(Customer.business_id == current_user.business)
        )
    ).scalars().all()
    ids = [c.id for c in customers]

    intelligence = {}
    tags_by_customer: dict[uuid.UUID, list[str]] = {}
    if ids:
        intelligence = {
            row.customer_id: row
            for row in (
                await db.execute(
                    select(CustomerIntelligence).where(CustomerIntelligence.customer_id.in_(ids))
                )
            ).scalars().all()
        }
        for customer_id, label in (
            await db.execute(
                select(CustomerTag.customer_id, CustomerTag.label).where(
                    CustomerTag.customer_id.in_(ids), CustomerTag.status == TagStatus.confirmed,
                )
            )
        ).all():
            tags_by_customer.setdefault(customer_id, []).append(label)

    by_stage: dict[str | None, list[Customer]] = {}
    for c in customers:
        by_stage.setdefault(c.stage, []).append(c)

    def _column(stage: str | None) -> PipelineColumn:
        members = by_stage.get(stage, [])
        cards = [
            PipelineCard(
                customer_id=str(c.id),
                name=c.display_name,
                deal_value_paise=c.deal_value_paise,
                health_score=intelligence[c.id].health_score if c.id in intelligence else None,
                tags=tags_by_customer.get(c.id, []),
            )
            for c in members
        ]
        total = sum(c.deal_value_paise or 0 for c in members)
        return PipelineColumn(stage=stage, total_deal_value_paise=total, customers=cards)

    # Unstaged first, then every configured stage in order, then any stage a
    # customer carries that has since been removed from the business's own
    # list - their card still needs somewhere to render, not to vanish.
    known = set(stage_order)
    leftover = sorted(s for s in by_stage if s is not None and s not in known)
    columns = [_column(None)] + [_column(s) for s in stage_order] + [_column(s) for s in leftover]

    return PipelineBoard(columns=columns)


@router.post("/tags/bulk", response_model=BulkTagResult)
async def bulk_add_tag(body: BulkTagIn, current_user: CurrentUserDep, db: DbDep) -> BulkTagResult:
    """
    Tag many customers at once - the operation a business with a real
    backlog reaches for, not the one a business with three customers needs.
    """
    label = body.label.strip().lower()
    if not label:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Label can't be empty")

    owned_ids = {
        row
        for (row,) in (
            await db.execute(
                select(Customer.id).where(
                    Customer.id.in_(body.customer_ids), Customer.business_id == current_user.business,
                )
            )
        ).all()
    }
    not_found = len(set(body.customer_ids)) - len(owned_ids)

    existing_by_customer = {
        t.customer_id: t
        for t in (
            await db.execute(
                select(CustomerTag).where(
                    CustomerTag.customer_id.in_(owned_ids), CustomerTag.label == label,
                )
            )
        ).scalars().all()
    }

    now = datetime.now(timezone.utc)
    tagged = 0
    already_tagged = 0
    for customer_id in owned_ids:
        existing = existing_by_customer.get(customer_id)
        if existing is not None:
            if existing.status == TagStatus.confirmed:
                already_tagged += 1
                continue
            existing.status = TagStatus.confirmed
            existing.decided_by_user_id = current_user.id
            existing.decided_at = now
            tagged += 1
            continue
        db.add(CustomerTag(
            business_id=current_user.business,
            customer_id=customer_id,
            label=label,
            status=TagStatus.confirmed,
            created_by_user_id=current_user.id,
            decided_by_user_id=current_user.id,
            decided_at=now,
        ))
        tagged += 1

    return BulkTagResult(tagged=tagged, already_tagged=already_tagged, not_found=not_found)
