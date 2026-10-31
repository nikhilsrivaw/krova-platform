"""
Website chat page: the sites a business runs KROVA's chat widget on. The visitor-facing side is widget.py.

A site is a domain (example.com - the widget also runs on www.example.com), the key its snippet carries, and how the
widget looks and behaves. The page also shows whether the code is working on the site, the last week's numbers, and
the recent chats - including the ones where the visitor never left a number, which never reach the Inbox.

Owner and admins only: adding a site lets KROVA's AI answer on a new public page, and anonymous visitors' chats are
read here.
"""

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from services.api.dependencies import DbDep, OwnerOrAdminDep
from shared.channels.web import sites
from shared.db.models import Channel, Customer, Direction, Message, WebSession, WebWidgetConfig

router = APIRouter(prefix="/web-chat", tags=["web-chat"])

MAX_SITES = 10
STATS_DAYS = 7


class SiteIn(BaseModel):
    domain: str = Field(min_length=3, max_length=255)
    title: str | None = Field(default=None, max_length=60)
    greeting: str | None = Field(default=None, max_length=300)
    accent: str | None = Field(default=None, max_length=7)
    active: bool = True
    position: str | None = None
    launcher_text: str | None = Field(default=None, max_length=30)
    teaser: str | None = Field(default=None, max_length=140)
    starters: list[str] = Field(default_factory=list, max_length=10)
    ask_contact: str | None = None
    language: str | None = None
    theme: str | None = None
    whatsapp_button: bool = False


class SiteOut(BaseModel):
    id: str
    domain: str
    site_key: str
    title: str | None
    greeting: str | None
    accent: str | None
    active: bool
    position: str
    launcher_text: str | None
    teaser: str | None
    starters: list[str]
    ask_contact: str
    language: str
    theme: str
    whatsapp_button: bool
    last_loaded_at: str | None
    # A page with the code loaded the widget in the last two days.
    working: bool


class StatsOut(BaseModel):
    days: int
    chats: int
    with_number: int
    needed_person: int


class ChatRow(BaseModel):
    id: str
    started_at: str
    last_seen_at: str
    site: str | None
    page_url: str | None
    first_message: str | None
    turns: int
    customer_id: str | None
    customer_name: str | None
    needed_person: bool


class ChatLine(BaseModel):
    role: str
    text: str
    at: str | None = None


class ChatDetail(BaseModel):
    id: str
    customer_id: str | None
    page_url: str | None
    messages: list[ChatLine]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def _out(site: WebWidgetConfig) -> SiteOut:
    loaded = site.last_loaded_at
    if loaded is not None and loaded.tzinfo is None:
        loaded = loaded.replace(tzinfo=timezone.utc)
    working = loaded is not None and datetime.now(timezone.utc) - loaded < sites.LOADED_RECENTLY
    return SiteOut(
        id=str(site.id), domain=site.allowed_domain, site_key=site.site_key, title=site.title, greeting=site.greeting,
        accent=site.accent, active=site.active, position=site.position or "right", launcher_text=site.launcher_text,
        teaser=site.teaser, starters=list(site.starters or []), ask_contact=site.ask_contact or "when_needed",
        language=site.language or "en", theme=site.theme or "dark", whatsapp_button=bool(site.whatsapp_button),
        last_loaded_at=_iso(loaded), working=working,
    )


def _domain(raw: str) -> str:
    domain = sites.clean_domain(raw)
    if domain is None:
        raise HTTPException(422, "Enter your website's address, like example.com.")
    return domain


def _text(value: str | None) -> str | None:
    return (value or "").strip() or None


def _look(site: WebWidgetConfig, body: SiteIn) -> None:
    site.title = _text(body.title)
    site.greeting = _text(body.greeting)
    site.accent = sites.clean_colour(body.accent)
    site.active = body.active
    site.position = sites.pick(body.position, sites.POSITIONS)
    site.launcher_text = _text(body.launcher_text)
    site.teaser = _text(body.teaser)
    site.starters = sites.clean_starters(body.starters)
    site.ask_contact = sites.pick(body.ask_contact, sites.ASK_CONTACT)
    site.language = sites.pick(body.language, sites.LANGUAGES)
    site.theme = sites.pick(body.theme, sites.THEMES)
    site.whatsapp_button = body.whatsapp_button


async def _site(db, business_id: uuid.UUID, site_id: uuid.UUID) -> WebWidgetConfig:
    site = await db.get(WebWidgetConfig, site_id)
    if site is None or site.business_id != business_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Site not found")
    return site


async def _taken(db, business_id: uuid.UUID, domain: str, except_id: uuid.UUID | None = None) -> bool:
    query = select(WebWidgetConfig.id).where(
        WebWidgetConfig.business_id == business_id, WebWidgetConfig.allowed_domain == domain)
    if except_id is not None:
        query = query.where(WebWidgetConfig.id != except_id)
    return (await db.execute(query)).first() is not None


@router.get("/sites", response_model=list[SiteOut])
async def list_sites(current_user: OwnerOrAdminDep, db: DbDep) -> list[SiteOut]:
    rows = (await db.execute(select(WebWidgetConfig).where(WebWidgetConfig.business_id == current_user.business)
                             .order_by(WebWidgetConfig.created_at))).scalars().all()
    return [_out(s) for s in rows]


@router.post("/sites", response_model=SiteOut, status_code=status.HTTP_201_CREATED)
async def add_site(body: SiteIn, current_user: OwnerOrAdminDep, db: DbDep) -> SiteOut:
    count = (await db.execute(select(func.count(WebWidgetConfig.id)).where(
        WebWidgetConfig.business_id == current_user.business))).scalar() or 0
    if count >= MAX_SITES:
        raise HTTPException(422, f"Up to {MAX_SITES} websites.")
    domain = _domain(body.domain)
    if await _taken(db, current_user.business, domain):
        raise HTTPException(409, "This website is already added.")
    site = WebWidgetConfig(business_id=current_user.business, site_key=secrets.token_urlsafe(18),
                           allowed_domain=domain)
    _look(site, body)
    db.add(site)
    await db.flush()
    return _out(site)


@router.put("/sites/{site_id}", response_model=SiteOut)
async def update_site(site_id: uuid.UUID, body: SiteIn, current_user: OwnerOrAdminDep, db: DbDep) -> SiteOut:
    site = await _site(db, current_user.business, site_id)
    domain = _domain(body.domain)
    if await _taken(db, current_user.business, domain, except_id=site.id):
        raise HTTPException(409, "This website is already added.")
    site.allowed_domain = domain
    _look(site, body)
    await db.flush()
    return _out(site)


@router.delete("/sites/{site_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_site(site_id: uuid.UUID, current_user: OwnerOrAdminDep, db: DbDep) -> None:
    """The snippet on that site stops working at once. Past website conversations stay in the Inbox."""
    site = await _site(db, current_user.business, site_id)
    await db.delete(site)


@router.get("/stats", response_model=StatsOut)
async def stats(current_user: OwnerOrAdminDep, db: DbDep) -> StatsOut:
    """The last week: chats started, how many left a number, how many needed a person."""
    since = datetime.now(timezone.utc) - timedelta(days=STATS_DAYS)
    row = (await db.execute(select(
        func.count(WebSession.id),
        func.count(WebSession.customer_id),
        func.count(WebSession.needed_person_at),
    ).where(
        WebSession.business_id == current_user.business, WebSession.consent_given_at.is_not(None),
        WebSession.created_at >= since,
    ))).one()
    return StatsOut(days=STATS_DAYS, chats=int(row[0] or 0), with_number=int(row[1] or 0),
                    needed_person=int(row[2] or 0))


async def _first_inbound(db, session: WebSession) -> tuple[str | None, int]:
    """The visitor's first message in this chat, and how many messages it has."""
    if session.customer_id is None:
        lines = [e for e in (session.transcript or []) if e.get("text")]
        first = next((e["text"] for e in lines if e.get("role") == "user"), None)
        return first, len(lines)
    base = select(Message).where(
        Message.customer_id == session.customer_id, Message.channel == Channel.web,
        Message.occurred_at >= session.created_at)
    first = (await db.execute(base.where(Message.direction == Direction.inbound)
                              .order_by(Message.occurred_at).limit(1))).scalars().first()
    count = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    return (first.content if first else None), int(count)


@router.get("/chats", response_model=list[ChatRow])
async def recent_chats(current_user: OwnerOrAdminDep, db: DbDep, limit: int = Query(default=30, ge=1, le=100)) -> list[ChatRow]:
    """Recent website chats, newest first - with or without a number."""
    rows = (await db.execute(
        select(WebSession, WebWidgetConfig.allowed_domain, Customer.display_name)
        .outerjoin(WebWidgetConfig, WebWidgetConfig.id == WebSession.site_id)
        .outerjoin(Customer, Customer.id == WebSession.customer_id)
        .where(WebSession.business_id == current_user.business, WebSession.consent_given_at.is_not(None))
        .order_by(WebSession.last_seen_at.desc()).limit(limit)
    )).all()
    out = []
    for session, domain, name in rows:
        first, turns = await _first_inbound(db, session)
        if turns == 0:
            continue  # accepted the notice and left without writing
        out.append(ChatRow(
            id=str(session.id), started_at=_iso(session.created_at) or "", last_seen_at=_iso(session.last_seen_at) or "",
            site=domain, page_url=session.page_url, first_message=(first or "")[:200] or None, turns=turns,
            customer_id=str(session.customer_id) if session.customer_id else None, customer_name=name,
            needed_person=session.needed_person_at is not None,
        ))
    return out


@router.get("/chats/{session_id}", response_model=ChatDetail)
async def chat_detail(session_id: uuid.UUID, current_user: OwnerOrAdminDep, db: DbDep) -> ChatDetail:
    """One website chat as the visitor saw it."""
    session = await db.get(WebSession, session_id)
    if session is None or session.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat not found")
    lines = await sites.history(db, session, limit=100)
    return ChatDetail(id=str(session.id), customer_id=str(session.customer_id) if session.customer_id else None,
                      page_url=session.page_url, messages=[ChatLine(role=l["role"], text=l["text"], at=l.get("at"))
                                                           for l in lines])
