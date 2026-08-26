"""
The knowledge base — and the gap list it exists to close.

The screen an owner opens after seeing "the agent couldn't answer this". It
shows what the agent has been asked and could not answer, and lets them fix
each one by adding what was missing.

That pairing is the whole point. A folder of uploaded PDFs is a chore nobody
completes; a list saying "seven customers asked about braces pricing and we
had to escalate" is a task with an obvious payoff.
"""

import io
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import (
    BusinessDNA,
    DraftAction,
    KnowledgeItem,
    KnowledgeKind,
    KnowledgeSource,
    MessageDraft,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

# Roughly four characters per token. Crude, and adequate for deciding when a
# client has outgrown whole-document injection.
CHARS_PER_TOKEN = 4

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
# Above this the whole-document approach stops being sensible and retrieval
# starts earning its complexity.
RETRIEVAL_THRESHOLD_TOKENS = 12_000


class KnowledgeIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=50_000)
    kind: Literal["price_list", "faq", "policy", "hours", "service", "other"] = "other"
    resolves_gap: str | None = None


class KnowledgeOut(BaseModel):
    id: str
    title: str
    kind: str
    source: str
    content: str
    filename: str | None
    token_estimate: int
    active: bool
    resolves_gap: str | None
    created_at: str


class GapOut(BaseModel):
    gap: str
    times_asked: int
    last_asked: str | None
    example_question: str | None


class KnowledgeStatus(BaseModel):
    items: int
    total_tokens: int
    fits_in_context: bool
    advice: str


def _value(v) -> str:
    return v.value if hasattr(v, "value") else str(v)


def _out(item: KnowledgeItem) -> KnowledgeOut:
    return KnowledgeOut(
        id=str(item.id),
        title=item.title,
        kind=_value(item.kind),
        source=_value(item.source),
        content=item.content,
        filename=item.filename,
        token_estimate=item.token_estimate,
        active=item.active,
        resolves_gap=item.resolves_gap,
        created_at=item.created_at.isoformat(),
    )


def _extract(raw: bytes, content_type: str | None, filename: str) -> str:
    """
    Pull readable text out of an upload.

    Plain text and CSV only for now. A PDF parser is a dependency and a class
    of failure - malformed files, scanned images with no text layer - and it
    is better to tell an owner plainly that we cannot read their PDF than to
    silently give the agent an empty document and let it escalate forever.
    """
    lowered = (filename or "").lower()
    if lowered.endswith((".txt", ".md", ".csv")) or (
        content_type and content_type.startswith("text/")
    ):
        return raw.decode("utf-8", errors="replace").strip()

    raise HTTPException(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        detail=(
            "Only text files are supported at the moment (.txt, .md, .csv). "
            "For a PDF or spreadsheet, copy the text in directly — it works "
            "just as well and we can be sure of what the agent reads."
        ),
    )


@router.get("", response_model=list[KnowledgeOut])
async def list_knowledge(current_user: CurrentUserDep, db: DbDep) -> list[KnowledgeOut]:
    result = await db.execute(
        select(KnowledgeItem)
        .where(KnowledgeItem.business_id == current_user.business)
        .order_by(KnowledgeItem.created_at.desc())
    )
    return [_out(i) for i in result.scalars().all()]


@router.get("/gaps", response_model=list[GapOut])
async def list_gaps(current_user: CurrentUserDep, db: DbDep) -> list[GapOut]:
    """
    What the agent has been asked and could not answer.

    The most valuable screen in the product for a new client, because it is
    the only one that tells them something they did not already know about
    their own business — which questions customers keep asking that nobody
    has written down.
    """
    result = await db.execute(
        select(MessageDraft)
        .where(
            MessageDraft.business_id == current_user.business,
            MessageDraft.action == DraftAction.escalate,
            MessageDraft.gap.isnot(None),
        )
        .order_by(MessageDraft.created_at.desc())
        .limit(200)
    )
    drafts = result.scalars().all()

    # Group near-duplicates. The same missing fact gets described slightly
    # differently each time, and showing it five times as five gaps would bury
    # the one that matters.
    grouped: dict[str, dict] = {}
    for draft in drafts:
        key = (draft.gap or "").strip().lower()[:70]
        if not key:
            continue
        entry = grouped.setdefault(
            key,
            {
                "gap": draft.gap.strip(),
                "times_asked": 0,
                "last_asked": draft.created_at,
                "example": None,
            },
        )
        entry["times_asked"] += 1
        if draft.created_at > entry["last_asked"]:
            entry["last_asked"] = draft.created_at
        if entry["example"] is None and draft.in_reply_to_id:
            from shared.db.models import Message

            source = await db.get(Message, draft.in_reply_to_id)
            if source and source.content:
                entry["example"] = source.content[:200]

    return sorted(
        (
            GapOut(
                gap=e["gap"],
                times_asked=e["times_asked"],
                last_asked=e["last_asked"].isoformat() if e["last_asked"] else None,
                example_question=e["example"],
            )
            for e in grouped.values()
        ),
        key=lambda g: g.times_asked,
        reverse=True,
    )


@router.get("/status", response_model=KnowledgeStatus)
async def knowledge_status(current_user: CurrentUserDep, db: DbDep) -> KnowledgeStatus:
    """
    How much the agent is carrying, and whether that is still sensible.

    The honest answer to "when do we need a vector database": when this says
    so, and not before.
    """
    result = await db.execute(
        select(
            func.count(KnowledgeItem.id),
            func.coalesce(func.sum(KnowledgeItem.token_estimate), 0),
        ).where(
            KnowledgeItem.business_id == current_user.business,
            KnowledgeItem.active == True,  # noqa: E712
        )
    )
    count, tokens = result.one()
    tokens = int(tokens)
    fits = tokens < RETRIEVAL_THRESHOLD_TOKENS

    if count == 0:
        advice = (
            "Nothing added yet. The agent answers from your business details "
            "alone — add your price list or FAQs and it can do more."
        )
    elif fits:
        advice = (
            f"All {count} item(s) go to the agent on every message. "
            "Nothing is missed and nothing is retrieved wrongly."
        )
    else:
        advice = (
            "Your knowledge base has outgrown what fits in one message. "
            "We should switch to retrieving only the relevant parts."
        )

    return KnowledgeStatus(
        items=int(count), total_tokens=tokens, fits_in_context=fits, advice=advice
    )


@router.post("", response_model=KnowledgeOut, status_code=status.HTTP_201_CREATED)
async def add_knowledge(
    body: KnowledgeIn, current_user: CurrentUserDep, db: DbDep
) -> KnowledgeOut:
    """Write something down for the agent."""
    item = KnowledgeItem(
        business_id=current_user.business,
        title=body.title.strip(),
        kind=KnowledgeKind(body.kind),
        source=KnowledgeSource.typed,
        content=body.content.strip(),
        token_estimate=len(body.content) // CHARS_PER_TOKEN,
        active=True,
        resolves_gap=body.resolves_gap,
    )
    db.add(item)
    await db.flush()

    if body.resolves_gap:
        await _clear_gap(current_user.business, body.resolves_gap, db)

    logger.info(
        "knowledge added business=%s kind=%s tokens=%s",
        current_user.business,
        body.kind,
        item.token_estimate,
    )
    return _out(item)


@router.post("/upload", response_model=KnowledgeOut, status_code=status.HTTP_201_CREATED)
async def upload_knowledge(
    current_user: CurrentUserDep,
    db: DbDep,
    file: UploadFile = File(...),
    title: str = Form(...),
    kind: str = Form(default="other"),
) -> KnowledgeOut:
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="That file is too large. Keep documents under 2 MB.",
        )

    text = _extract(raw, file.content_type, file.filename or "")
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="We could not read any text from that file.",
        )

    item = KnowledgeItem(
        business_id=current_user.business,
        title=title.strip(),
        kind=KnowledgeKind(kind) if kind in KnowledgeKind._value2member_map_ else KnowledgeKind.other,
        source=KnowledgeSource.upload,
        content=text,
        filename=file.filename,
        content_type=file.content_type,
        token_estimate=len(text) // CHARS_PER_TOKEN,
        active=True,
    )
    db.add(item)
    await db.flush()
    return _out(item)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge(
    item_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep
) -> None:
    item = await db.get(KnowledgeItem, item_id)
    if item is None or item.business_id != current_user.business:
        raise HTTPException(status_code=404, detail="Not found")
    await db.delete(item)


async def _clear_gap(business_id: uuid.UUID, gap: str, db: DbDep) -> None:
    """
    Remove a gap the owner has just answered.

    Closing the loop visibly matters: an owner who fixes something and watches
    it disappear from the list will fix the next one.
    """
    dna = await db.get(BusinessDNA, business_id)
    if dna is None:
        return
    known = dict(dna.known_gaps or {})
    learned = [
        g for g in known.get("learned", []) if g.strip().lower() != gap.strip().lower()
    ]
    known["learned"] = learned
    dna.known_gaps = known
