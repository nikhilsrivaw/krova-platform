"""
Website chat page: the sites a business runs KROVA's chat widget on. The visitor-facing side is widget.py.

A site is a domain (example.com - the widget also runs on www.example.com), the key its snippet carries, and how the
widget looks. Owner and admins only: adding a site lets KROVA's AI answer on a new public page.
"""

import secrets
import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from services.api.dependencies import DbDep, OwnerOrAdminDep
from shared.channels.web import sites
from shared.db.models import WebWidgetConfig

router = APIRouter(prefix="/web-chat", tags=["web-chat"])

MAX_SITES = 10


class SiteIn(BaseModel):
    domain: str = Field(min_length=3, max_length=255)
    title: str | None = Field(default=None, max_length=60)
    greeting: str | None = Field(default=None, max_length=300)
    accent: str | None = Field(default=None, max_length=7)
    active: bool = True


class SiteOut(BaseModel):
    id: str
    domain: str
    site_key: str
    title: str | None
    greeting: str | None
    accent: str | None
    active: bool


def _out(site: WebWidgetConfig) -> SiteOut:
    return SiteOut(id=str(site.id), domain=site.allowed_domain, site_key=site.site_key, title=site.title,
                   greeting=site.greeting, accent=site.accent, active=site.active)


def _domain(raw: str) -> str:
    domain = sites.clean_domain(raw)
    if domain is None:
        raise HTTPException(422, "Enter your website's address, like example.com.")
    return domain


def _look(site: WebWidgetConfig, body: SiteIn) -> None:
    site.title = (body.title or "").strip() or None
    site.greeting = (body.greeting or "").strip() or None
    site.accent = sites.clean_colour(body.accent)
    site.active = body.active


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
