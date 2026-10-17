"""
The knowledge base — and the gap list it exists to close.

The screen an owner opens after seeing "the agent couldn't answer this". It
shows what the agent has been asked and could not answer, and lets them fix
each one by adding what was missing.

That pairing is the whole point. A folder of uploaded PDFs is a chore nobody
completes; a list saying "seven customers asked about braces pricing and we
had to escalate" is a task with an obvious payoff.
"""

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from services.api.dependencies import CurrentUserDep, DbDep, OwnerOrAdminDep
from shared import verticals
from shared.billing import limits as plan_limits
from shared.verticals import knowledge_card, knowledge_lookup
from shared.ai import knowledge_reader
from shared.ai.client import AIError
from shared.db.models import (
    Business,
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

# Above this the whole-document approach stops being sensible and retrieval
# starts earning its complexity.
RETRIEVAL_THRESHOLD_TOKENS = 12_000


class KnowledgeIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=knowledge_card.OTHER_CHARS_LIMIT)
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
    topic: str | None = None
    valid_until: str | None = None
    # "faq" for a top question; None for a card field (see topic) or a free note
    card: str | None = None


class GapOut(BaseModel):
    gap: str
    times_asked: int
    last_asked: str | None
    example_question: str | None
    # The card field this most likely belongs to ("when", "payment", "team"...). A suggestion only.
    suggested_field: str | None = None


class KnowledgeStatus(BaseModel):
    items: int
    total_tokens: int
    fits_in_context: bool
    other_items: int = 0
    other_tokens: int = 0
    other_items_limit: int = knowledge_card.OTHER_ITEMS_LIMIT
    other_tokens_limit: int = knowledge_card.OTHER_TOKENS_LIMIT
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
        topic=item.topic,
        valid_until=item.valid_until.isoformat() if item.valid_until else None,
        card=(item.extra or {}).get("card"),
    )


@router.get("", response_model=list[KnowledgeOut])
async def list_knowledge(current_user: OwnerOrAdminDep, db: DbDep) -> list[KnowledgeOut]:
    result = await db.execute(
        select(KnowledgeItem)
        .where(KnowledgeItem.business_id == current_user.business)
        .order_by(KnowledgeItem.created_at.desc())
    )
    return [_out(i) for i in result.scalars().all()]


def _same_gap(a: str, b: str) -> bool:
    """The same missing fact, worded slightly differently: one contains the other's opening words."""
    x, y = (a or "").strip().lower(), (b or "").strip().lower()
    return bool(x and y) and (x[:60] in y or y[:60] in x)


@router.get("/gaps", response_model=list[GapOut])
async def list_gaps(current_user: OwnerOrAdminDep, db: DbDep) -> list[GapOut]:
    """
    What the agent has been asked and could not answer, on text AND on calls.

    The list is the business's own record of gaps (BusinessDNA.known_gaps["learned"], written whenever the
    agent hands something over, by text or by voice). Answering a gap, or dismissing it, removes it from
    that record, so it stays gone. How often it was asked, when, and an example message come from the
    escalated drafts that name the same gap.
    """
    dna = await db.get(BusinessDNA, current_user.business)
    learned = list((dna.known_gaps or {}).get("learned", [])) if dna else []
    if not learned:
        return []
    drafts = (
        await db.execute(
            select(MessageDraft)
            .where(
                MessageDraft.business_id == current_user.business,
                MessageDraft.action == DraftAction.escalate,
                MessageDraft.gap.isnot(None),
            )
            .order_by(MessageDraft.created_at.desc())
            .limit(500)
        )
    ).scalars().all()
    out: list[GapOut] = []
    for gap in learned[:100]:
        matches = [d for d in drafts if _same_gap(gap, d.gap or "")]
        example = None
        for d in matches:
            if d.in_reply_to_id:
                from shared.db.models import Message

                source = await db.get(Message, d.in_reply_to_id)
                if source and source.content:
                    example = source.content[:200]
                    break
        out.append(
            GapOut(
                gap=gap.strip(),
                times_asked=max(len(matches), 1),
                last_asked=matches[0].created_at.isoformat() if matches else None,
                example_question=example,
                suggested_field=knowledge_card.suggest_field(f"{gap} {example or ''}"),
            )
        )
    return sorted(out, key=lambda g: g.times_asked, reverse=True)


class GapDismissIn(BaseModel):
    gap: str = Field(min_length=1, max_length=500)


@router.post("/gaps/handover", status_code=status.HTTP_204_NO_CONTENT)
async def handover_gap(body: GapDismissIn, current_user: OwnerOrAdminDep, db: DbDep) -> None:
    """
    "Always hand this to me": from now on the agent passes this kind of question to a person instead of answering.
    Kept short and owner-made, beside the template's own list (shared/ai/context.py::standing_gaps).
    """
    dna = await db.get(BusinessDNA, current_user.business)
    if dna is None:
        return
    known = dict(dna.known_gaps or {})
    rules = list(known.get("owner_rules", []))
    if not any(_same_gap(r, body.gap) for r in rules):
        rules.append(body.gap.strip()[:200])
    known["owner_rules"] = rules[-30:]
    known["learned"] = [g for g in known.get("learned", []) if not _same_gap(g, body.gap)]
    dna.known_gaps = known


@router.post("/gaps/dismiss", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_gap(body: GapDismissIn, current_user: OwnerOrAdminDep, db: DbDep) -> None:
    """Take a gap off the list without writing anything: it is not a real gap, or it is already covered."""
    await _clear_gap(current_user.business, body.gap, db)


@router.get("/status", response_model=KnowledgeStatus)
async def knowledge_status(current_user: OwnerOrAdminDep, db: DbDep) -> KnowledgeStatus:
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

    others = await _active_others(db, current_user.business)
    return KnowledgeStatus(
        items=int(count), total_tokens=tokens, fits_in_context=fits, advice=advice,
        other_items=len(others), other_tokens=sum(t for _, t in others),
    )


async def _check_room(db, business_id: uuid.UUID, new_tokens: int, exclude_id: uuid.UUID | None = None) -> None:
    """The AI reads all active knowledge before every reply, so each plan has a ceiling on how much."""
    business = await db.get(Business, business_id)
    if business is None or plan_limits.limits_for(business.plan) is None:
        return
    used = (
        await db.execute(
            select(func.coalesce(func.sum(KnowledgeItem.token_estimate), 0)).where(
                KnowledgeItem.business_id == business_id,
                KnowledgeItem.active == True,  # noqa: E712
                *([KnowledgeItem.id != exclude_id] if exclude_id else []),
            )
        )
    ).scalar_one()
    plan_limits.check_size(business.plan, "knowledge_tokens", "tokens of business information the AI reads (about 670 tokens a page)", int(used) + new_tokens)


async def _active_others(db, business_id: uuid.UUID, exclude_id: uuid.UUID | None = None) -> list[tuple[uuid.UUID, int]]:
    rows = (
        await db.execute(
            select(KnowledgeItem).where(KnowledgeItem.business_id == business_id, KnowledgeItem.active == True)  # noqa: E712
        )
    ).scalars().all()
    return [(r.id, r.token_estimate) for r in rows if knowledge_card.is_other(r) and r.id != exclude_id]


async def _check_other_room(db, business_id: uuid.UUID, new_tokens: int, exclude_id: uuid.UUID | None = None) -> None:
    """Free notes have their own small budget, inside the plan's: they are for what fits no card field."""
    problem = knowledge_card.check_other_room(await _active_others(db, business_id, exclude_id), new_tokens)
    if problem:
        raise HTTPException(status_code=422, detail=problem)


@router.post("", response_model=KnowledgeOut, status_code=status.HTTP_201_CREATED)
async def add_knowledge(
    body: KnowledgeIn, current_user: OwnerOrAdminDep, db: DbDep
) -> KnowledgeOut:
    """Write a short free note for the agent - something that fits no card field."""
    await _check_other_room(db, current_user.business, len(body.content) // CHARS_PER_TOKEN)
    await _check_room(db, current_user.business, len(body.content) // CHARS_PER_TOKEN)
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


FILE_READS_PER_DAY = 5


class ReadFieldOut(BaseModel):
    key: str
    title: str
    limit: int
    current: str | None
    suggested: str


class ReadFaqOut(BaseModel):
    question: str
    answer: str


class ReadRowOut(BaseModel):
    name: str
    price: str
    details: str


class ReadFileOut(BaseModel):
    fields: list[ReadFieldOut]
    faqs: list[ReadFaqOut]
    truncated: bool
    reads_left_today: int
    # the longer, opened-when-needed knowledge the same file holds
    catalogue: list[ReadRowOut] = []
    catalogue_now: int = 0
    bank: list[ReadFaqOut] = []
    bank_room: int = 0


def _take_file_read(business: Business) -> int:
    """Count one file read for today; returns how many are left. Each read is one AI call, so a day has a few."""
    today = datetime.now(timezone.utc).date().isoformat()
    settings_ = dict(business.settings or {})
    used = settings_.get("knowledge_file_reads") or {}
    count = used.get("count", 0) if used.get("day") == today else 0
    if count >= FILE_READS_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=f"You can read {FILE_READS_PER_DAY} files a day. Try again tomorrow, or type it into the card.",
        )
    settings_["knowledge_file_reads"] = {"day": today, "count": count + 1}
    business.settings = settings_
    return FILE_READS_PER_DAY - count - 1


@router.post("/read-file", response_model=ReadFileOut)
async def read_file(current_user: OwnerOrAdminDep, db: DbDep, file: UploadFile = File(...)) -> ReadFileOut:
    """
    Read a price list, menu, brochure or FAQ sheet (PDF, photo or text) ONCE and suggest what goes on the card.
    Nothing is saved and the file is not kept: the owner checks the suggestions and saves the ones they want.
    The AI then reads only the short card on each message, never the whole file.
    """
    raw = await file.read()
    try:
        knowledge_reader.media_type_for(file.filename or "", file.content_type)
    except knowledge_reader.UnreadableFile as exc:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)) from exc
    if len(raw) > knowledge_reader.MAX_FILE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="That file is too large. Keep it under 5 MB.")
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status_code=404, detail="Not found")
    # A read that fails below raises, and get_db rolls back on any exception, so a failed read is not counted.
    left = _take_file_read(business)

    vertical = business.vertical or "general"
    rows = (await db.execute(select(KnowledgeItem).where(KnowledgeItem.business_id == current_user.business))).scalars().all()
    by_topic = {r.topic: r for r in rows if r.topic}
    faq_room = knowledge_card.FAQ_LIMIT - sum(1 for r in rows if _is_faq(r))
    bank_room = knowledge_lookup.BANK_LIMIT - sum(1 for r in rows if _is_bank(r))
    catalogue_item = by_topic.get(knowledge_lookup.CATALOGUE_TOPIC)
    catalogue_now = len((catalogue_item.extra or {}).get("catalogue") or []) if catalogue_item else 0
    try:
        reading = await knowledge_reader.read_into_card(
            raw=raw, filename=file.filename or "", content_type=file.content_type, vertical=vertical, faq_room=faq_room,
            bank_room=bank_room,
        )
    except knowledge_reader.UnreadableFile as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except AIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not read the file right now. Try again in a minute.") from exc
    await db.flush()
    logger.info(
        "knowledge file read business=%s fields=%s faqs=%s cost_paise=%s",
        current_user.business, len(reading.fields), len(reading.faqs), reading.cost_paise,
    )

    return ReadFileOut(
        fields=[
            ReadFieldOut(
                key=f["key"], title=f["title"], limit=f["limit"],
                current=by_topic[f["key"]].content if f["key"] in by_topic else None,
                suggested=reading.fields[f["key"]],
            )
            for f in knowledge_card.fields_for(vertical) if f["key"] in reading.fields
        ],
        faqs=[ReadFaqOut(question=q, answer=a) for q, a in reading.faqs],
        truncated=reading.truncated,
        reads_left_today=left,
        catalogue=[ReadRowOut(**r) for r in reading.catalogue],
        catalogue_now=catalogue_now,
        bank=[ReadFaqOut(question=q, answer=a) for q, a in reading.bank],
        bank_room=bank_room,
    )


class KnowledgePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, min_length=1, max_length=knowledge_card.OTHER_CHARS_LIMIT)
    kind: Literal["price_list", "faq", "policy", "hours", "service", "other"] | None = None
    active: bool | None = None
    valid_until: datetime | None = None
    clear_valid_until: bool = False


@router.patch("/{item_id}", response_model=KnowledgeOut)
async def update_knowledge(
    item_id: uuid.UUID, patch: KnowledgePatch, current_user: OwnerOrAdminDep, db: DbDep
) -> KnowledgeOut:
    """Edit an item, switch it off, or give it an end date. An item that is off is not sent to the agent."""
    item = await db.get(KnowledgeItem, item_id)
    if item is None or item.business_id != current_user.business:
        raise HTTPException(status_code=404, detail="Not found")
    content = patch.content.strip() if patch.content is not None else item.content
    active = item.active if patch.active is None else patch.active
    lookup = _is_lookup(item)
    if item.topic == knowledge_lookup.CATALOGUE_TOPIC and patch.content is not None:
        raise HTTPException(status_code=422, detail="Edit the catalogue on its own tab.")
    if _is_bank(item) and len(content) > knowledge_lookup.BANK_ANSWER_LIMIT:
        raise HTTPException(status_code=422, detail=f"Keep an answer to {knowledge_lookup.BANK_ANSWER_LIMIT} characters.")
    tokens = 0 if lookup else len(content) // CHARS_PER_TOKEN
    if not lookup and active and (not item.active or tokens > item.token_estimate):
        if knowledge_card.is_other(item):
            await _check_other_room(db, current_user.business, tokens, exclude_id=item.id)
        await _check_room(db, current_user.business, tokens, exclude_id=item.id)
    if patch.title is not None:
        item.title = patch.title.strip()
    if patch.kind is not None:
        item.kind = KnowledgeKind(patch.kind)
    item.content, item.token_estimate, item.active = content, tokens, active
    if patch.clear_valid_until:
        item.valid_until = None
    elif patch.valid_until is not None:
        item.valid_until = patch.valid_until
    await db.flush()
    return _out(item)


class CardFieldOut(BaseModel):
    key: str
    title: str
    question: str
    placeholder: str
    kind: str
    limit: int
    required: bool
    expires: bool
    link: bool
    value: str | None
    item_id: str | None
    valid_until: str | None


class TeamPerson(BaseModel):
    name: str = Field(min_length=1, max_length=knowledge_card.TEAM_LIMITS["name"])
    role: str = Field(default="", max_length=knowledge_card.TEAM_LIMITS["role"])
    handles: str = Field(default="", max_length=knowledge_card.TEAM_LIMITS["handles"])
    languages: str = Field(default="", max_length=knowledge_card.TEAM_LIMITS["languages"])


class TeamCardIn(BaseModel):
    contact: str = Field(default="", max_length=knowledge_card.TEAM_TEXT_LIMITS["contact"])
    people: list[TeamPerson] = Field(default_factory=list, max_length=knowledge_card.TEAM_PEOPLE_LIMIT)
    languages: str = Field(default="", max_length=knowledge_card.TEAM_TEXT_LIMITS["languages"])
    response_note: str = Field(default="", max_length=knowledge_card.TEAM_TEXT_LIMITS["response_note"])
    resolves_gap: str | None = None


class TeamCardOut(BaseModel):
    contact: str
    people: list[TeamPerson]
    languages: str
    response_note: str
    item_id: str | None


class FaqOut(BaseModel):
    id: str
    question: str
    answer: str
    active: bool


class CardCoverage(BaseModel):
    required_total: int
    required_done: int
    fields_total: int
    fields_done: int
    team_done: int
    faqs: int
    faq_limit: int


class CardOut(BaseModel):
    vertical: str
    vertical_label: str
    fields: list[CardFieldOut]
    team: TeamCardOut
    faqs: list[FaqOut]
    coverage: CardCoverage
    tokens: int  # what the cards cost the AI to read on every message, roughly


async def _business_vertical(db, business_id: uuid.UUID) -> str:
    business = await db.get(Business, business_id)
    return business.vertical if business is not None else "general"


def _is_faq(item) -> bool:
    return (item.extra or {}).get("card") == "faq"


def _is_bank(item) -> bool:
    return (item.extra or {}).get("card") == knowledge_lookup.BANK


def _is_lookup(item) -> bool:
    """Opened only when a message matches it (Catalogue, Question bank) - never read whole, so free of the plan budget."""
    return item.topic == knowledge_lookup.CATALOGUE_TOPIC or _is_bank(item)


@router.get("/card", response_model=CardOut)
async def card(current_user: OwnerOrAdminDep, db: DbDep) -> CardOut:
    """
    The Business Card, the Team Card and the top questions - the short, same-for-every-type knowledge the AI reads on
    every message. The business type only rewords the fields (its template's knowledge_card block).
    """
    vertical = await _business_vertical(db, current_user.business)
    fields = knowledge_card.fields_for(vertical)
    rows = (await db.execute(select(KnowledgeItem).where(KnowledgeItem.business_id == current_user.business))).scalars().all()
    by_topic = {r.topic: r for r in rows if r.topic}
    faqs = [r for r in rows if _is_faq(r)]
    team_item = by_topic.get(knowledge_card.TEAM_TOPIC)
    team = dict((team_item.extra or {}).get("team_card") or {}) if team_item else {}
    answered = {f["key"] for f in fields if f["key"] in by_topic and by_topic[f["key"]].content.strip()}
    try:
        label = verticals.get(vertical).get("label", vertical)
    except verticals.UnknownVertical:
        label = vertical
    tokens = sum(
        r.token_estimate for r in rows
        if r.active and (r.topic in knowledge_card.FIELD_KEYS or r.topic == knowledge_card.TEAM_TOPIC or _is_faq(r))
    )
    return CardOut(
        vertical=vertical, vertical_label=label,
        fields=[
            CardFieldOut(
                key=f["key"], title=f["title"], question=f["question"], placeholder=f["placeholder"], kind=f["kind"],
                limit=f["limit"], required=f["required"], expires=f["expires"], link=f["link"],
                value=by_topic[f["key"]].content if f["key"] in by_topic else None,
                item_id=str(by_topic[f["key"]].id) if f["key"] in by_topic else None,
                valid_until=by_topic[f["key"]].valid_until.isoformat() if f["key"] in by_topic and by_topic[f["key"]].valid_until else None,
            )
            for f in fields
        ],
        team=TeamCardOut(
            contact=team.get("contact", ""), people=[TeamPerson(**p) for p in team.get("people", [])],
            languages=team.get("languages", ""), response_note=team.get("response_note", ""),
            item_id=str(team_item.id) if team_item else None,
        ),
        faqs=[FaqOut(id=str(r.id), question=r.title, answer=r.content, active=r.active) for r in faqs],
        coverage=CardCoverage(**knowledge_card.coverage(fields, answered, team_item is not None, len(faqs))),
        tokens=tokens,
    )


class CardFieldIn(BaseModel):
    content: str = Field(max_length=2000)
    valid_until: datetime | None = None
    resolves_gap: str | None = None


async def _topic_item(db, business_id: uuid.UUID, topic: str):
    return (
        await db.execute(select(KnowledgeItem).where(KnowledgeItem.business_id == business_id, KnowledgeItem.topic == topic))
    ).scalar_one_or_none()


@router.put("/card/{key}", response_model=KnowledgeOut | None)
async def save_card_field(key: str, body: CardFieldIn, current_user: OwnerOrAdminDep, db: DbDep) -> KnowledgeOut | None:
    """Save one Business Card field. Empty removes it. Each field has a character limit, because the AI reads it every time."""
    vertical = await _business_vertical(db, current_user.business)
    field = next((f for f in knowledge_card.fields_for(vertical) if f["key"] == key), None)
    if field is None:
        raise HTTPException(status_code=404, detail="That is not a card field.")
    content = body.content.strip()
    problem = knowledge_card.check_value(field, content)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    existing = await _topic_item(db, current_user.business, key)
    if not content:
        if existing is not None:
            await db.delete(existing)
        return None
    tokens = len(content) // CHARS_PER_TOKEN
    await _check_room(db, current_user.business, tokens, exclude_id=existing.id if existing else None)
    valid_until = body.valid_until if field["expires"] else None
    if existing is None:
        existing = KnowledgeItem(
            business_id=current_user.business, title=field["title"], kind=KnowledgeKind(field["kind"]),
            source=KnowledgeSource.typed, content=content, token_estimate=tokens, active=True, topic=key, valid_until=valid_until,
        )
        db.add(existing)
    else:
        existing.content, existing.token_estimate, existing.valid_until = content, tokens, valid_until
        existing.title, existing.kind, existing.active = field["title"], KnowledgeKind(field["kind"]), True
    if body.resolves_gap:
        existing.resolves_gap = body.resolves_gap
        await _clear_gap(current_user.business, body.resolves_gap, db)
    await db.flush()
    return _out(existing)


@router.put("/team-card", response_model=TeamCardOut)
async def save_team_card(body: TeamCardIn, current_user: OwnerOrAdminDep, db: DbDep) -> TeamCardOut:
    """
    Who customers can ask for, how to reach you, languages, when you reply. Saved as one short item the AI reads;
    the rows are kept so the card can be edited.
    """
    card_data = {
        "contact": body.contact.strip(), "languages": body.languages.strip(), "response_note": body.response_note.strip(),
        "people": [p.model_dump() for p in body.people if p.name.strip()],
    }
    text = knowledge_card.render_team(card_data)
    existing = await _topic_item(db, current_user.business, knowledge_card.TEAM_TOPIC)
    if not text:
        if existing is not None:
            await db.delete(existing)
        return TeamCardOut(contact="", people=[], languages="", response_note="", item_id=None)
    tokens = len(text) // CHARS_PER_TOKEN
    await _check_room(db, current_user.business, tokens, exclude_id=existing.id if existing else None)
    if existing is None:
        existing = KnowledgeItem(
            business_id=current_user.business, title="Our team", kind=KnowledgeKind.other, source=KnowledgeSource.typed,
            content=text, token_estimate=tokens, active=True, topic=knowledge_card.TEAM_TOPIC,
        )
        db.add(existing)
    else:
        existing.content, existing.token_estimate, existing.active = text, tokens, True
    existing.extra = {**(existing.extra or {}), "team_card": card_data}
    if body.resolves_gap:
        await _clear_gap(current_user.business, body.resolves_gap, db)
    await db.flush()
    return TeamCardOut(
        contact=card_data["contact"], people=[TeamPerson(**p) for p in card_data["people"]],
        languages=card_data["languages"], response_note=card_data["response_note"], item_id=str(existing.id),
    )


class FaqIn(BaseModel):
    question: str = Field(min_length=1, max_length=knowledge_card.FAQ_QUESTION_LIMIT)
    answer: str = Field(min_length=1, max_length=knowledge_card.FAQ_ANSWER_LIMIT)
    resolves_gap: str | None = None


@router.post("/faqs", response_model=FaqOut, status_code=status.HTTP_201_CREATED)
async def add_faq(body: FaqIn, current_user: OwnerOrAdminDep, db: DbDep) -> FaqOut:
    """One short question and answer. At most FAQ_LIMIT: past that, replace the least useful one instead of adding."""
    rows = (await db.execute(select(KnowledgeItem).where(KnowledgeItem.business_id == current_user.business))).scalars().all()
    if sum(1 for r in rows if _is_faq(r)) >= knowledge_card.FAQ_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=f"You already have {knowledge_card.FAQ_LIMIT} top questions. Edit or remove one first - the AI reads them on every message.",
        )
    answer = body.answer.strip()
    tokens = (len(body.question) + len(answer)) // CHARS_PER_TOKEN
    await _check_room(db, current_user.business, tokens)
    item = KnowledgeItem(
        business_id=current_user.business, title=body.question.strip(), kind=KnowledgeKind.faq, source=KnowledgeSource.typed,
        content=answer, token_estimate=tokens, active=True, resolves_gap=body.resolves_gap, extra={"card": "faq"},
    )
    db.add(item)
    if body.resolves_gap:
        await _clear_gap(current_user.business, body.resolves_gap, db)
    await db.flush()
    return FaqOut(id=str(item.id), question=item.title, answer=item.content, active=True)


# ── opened when needed: Catalogue and Question bank (shared/verticals/knowledge_lookup.py) ──────────────────────

class CatalogueRow(BaseModel):
    name: str = Field(min_length=1, max_length=knowledge_lookup.CATALOGUE_LIMITS["name"])
    price: str = Field(default="", max_length=knowledge_lookup.CATALOGUE_LIMITS["price"])
    details: str = Field(default="", max_length=knowledge_lookup.CATALOGUE_LIMITS["details"])


class CatalogueIn(BaseModel):
    rows: list[CatalogueRow] = Field(default_factory=list, max_length=knowledge_lookup.CATALOGUE_ROWS_LIMIT)


class CatalogueOut(BaseModel):
    rows: list[CatalogueRow]
    limit: int = knowledge_lookup.CATALOGUE_ROWS_LIMIT


@router.get("/catalogue", response_model=CatalogueOut)
async def get_catalogue(current_user: OwnerOrAdminDep, db: DbDep) -> CatalogueOut:
    item = await _topic_item(db, current_user.business, knowledge_lookup.CATALOGUE_TOPIC)
    rows = knowledge_lookup.clean_rows((item.extra or {}).get("catalogue") or []) if item else []
    return CatalogueOut(rows=[CatalogueRow(**r) for r in rows])


@router.put("/catalogue", response_model=CatalogueOut)
async def save_catalogue(body: CatalogueIn, current_user: OwnerOrAdminDep, db: DbDep) -> CatalogueOut:
    """
    Replace the whole catalogue. Up to CATALOGUE_ROWS_LIMIT rows; the AI is shown only the few a customer's message
    names, so a long catalogue does not make every reply dearer. No rows removes it.
    """
    rows = knowledge_lookup.clean_rows([r.model_dump() for r in body.rows])
    existing = await _topic_item(db, current_user.business, knowledge_lookup.CATALOGUE_TOPIC)
    if not rows:
        if existing is not None:
            await db.delete(existing)
        return CatalogueOut(rows=[])
    text = "\n".join(knowledge_lookup.row_line(r) for r in rows)
    if existing is None:
        db.add(KnowledgeItem(
            business_id=current_user.business, title="Catalogue", kind=KnowledgeKind.price_list,
            source=KnowledgeSource.typed, content=text, token_estimate=0, active=True,
            topic=knowledge_lookup.CATALOGUE_TOPIC, extra={"catalogue": rows},
        ))
    else:
        existing.content, existing.token_estimate, existing.active = text, 0, True
        existing.extra = {"catalogue": rows}
    await db.flush()
    return CatalogueOut(rows=[CatalogueRow(**r) for r in rows])


class BankIn(BaseModel):
    question: str = Field(min_length=1, max_length=knowledge_lookup.BANK_QUESTION_LIMIT)
    answer: str = Field(min_length=1, max_length=knowledge_lookup.BANK_ANSWER_LIMIT)
    resolves_gap: str | None = None


class BankOut(BaseModel):
    items: list[FaqOut]
    limit: int = knowledge_lookup.BANK_LIMIT


@router.get("/bank", response_model=BankOut)
async def get_bank(current_user: OwnerOrAdminDep, db: DbDep) -> BankOut:
    rows = (await db.execute(select(KnowledgeItem).where(KnowledgeItem.business_id == current_user.business))).scalars().all()
    bank = sorted((r for r in rows if _is_bank(r)), key=lambda r: r.created_at or datetime.min.replace(tzinfo=timezone.utc))
    return BankOut(items=[FaqOut(id=str(r.id), question=r.title, answer=r.content, active=r.active) for r in bank])


@router.post("/bank", response_model=FaqOut, status_code=status.HTTP_201_CREATED)
async def add_to_bank(body: BankIn, current_user: OwnerOrAdminDep, db: DbDep) -> FaqOut:
    """One more saved answer, shown to the AI only when a customer asks something like it."""
    rows = (await db.execute(select(KnowledgeItem).where(KnowledgeItem.business_id == current_user.business))).scalars().all()
    if sum(1 for r in rows if _is_bank(r)) >= knowledge_lookup.BANK_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=f"The question bank holds {knowledge_lookup.BANK_LIMIT} answers. Remove one you no longer need first.",
        )
    item = KnowledgeItem(
        business_id=current_user.business, title=body.question.strip(), kind=KnowledgeKind.faq,
        source=KnowledgeSource.typed, content=body.answer.strip(), token_estimate=0, active=True,
        resolves_gap=body.resolves_gap, extra={"card": knowledge_lookup.BANK},
    )
    db.add(item)
    if body.resolves_gap:
        await _clear_gap(current_user.business, body.resolves_gap, db)
    await db.flush()
    return FaqOut(id=str(item.id), question=item.title, answer=item.content, active=True)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge(
    item_id: uuid.UUID, current_user: OwnerOrAdminDep, db: DbDep
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
    learned = [g for g in known.get("learned", []) if not _same_gap(g, gap)]
    known["learned"] = learned
    dna.known_gaps = known
