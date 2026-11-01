"""
Feedback page: when and how KROVA asks customers how it went, the WhatsApp template it needs, and what they said.
The rules live in shared/care/feedback.py.

Owner and admins only: it decides messages and calls that go out to customers, and shows what customers said about
the team.
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import DbDep, OwnerOrAdminDep
from shared.care import feedback
from shared.channels.whatsapp import required_templates, template_service
from shared.db.models import Business, Customer, FeedbackRequest
from shared.scheduling.recall import type_allows_reviews
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/feedback", tags=["feedback"])


class SettingsIn(BaseModel):
    enabled: bool
    triggers: list[str] = Field(default_factory=list, max_length=10)
    delay_minutes: int = Field(ge=0, le=7 * 24 * 60)
    channel: str
    call_after_hours: int = Field(ge=1, le=72)
    cooldown_days: int = Field(ge=0, le=90)
    ask_review: bool = True


class SettingsOut(SettingsIn):
    trigger_labels: dict[str, str]
    # "approved" | "pending" | "rejected" | "missing" - the WhatsApp template's state at Meta.
    template_state: str
    template_reason: str | None
    review_link_set: bool
    # False for a type that may not ask for reviews (healthcare).
    review_allowed: bool


class StatsOut(BaseModel):
    days: int
    asked: int
    answered: int
    average: float | None
    happy: int
    unhappy: int
    spread: dict[str, int]


class ResponseOut(BaseModel):
    id: str
    customer_id: str
    customer_name: str | None
    trigger: str
    about: str
    channel: str
    status: str
    rating: int | None
    comment: str | None
    answered_via: str | None
    answered_at: datetime | None
    created_at: datetime
    note: str | None


class TemplateOut(BaseModel):
    created: list[str]
    already_there: list[str]
    failed: list[dict]


async def _settings_out(db, business: Business) -> SettingsOut:
    state, reason = (await required_templates.statuses(db, business.id)).get(feedback.TEMPLATE, ("missing", None))
    return SettingsOut(
        **feedback.read(business), trigger_labels=feedback.TRIGGERS, template_state=state, template_reason=reason,
        review_link_set=bool((business.settings or {}).get("google_review_url")),
        review_allowed=type_allows_reviews(business),
    )


@router.get("/settings", response_model=SettingsOut)
async def get_settings(current_user: OwnerOrAdminDep, db: DbDep) -> SettingsOut:
    return await _settings_out(db, await db.get(Business, current_user.business))


@router.put("/settings", response_model=SettingsOut)
async def put_settings(body: SettingsIn, current_user: OwnerOrAdminDep, db: DbDep) -> SettingsOut:
    if body.channel not in feedback.CHANNELS:
        raise HTTPException(422, "Ask on WhatsApp, by call, or both.")
    unknown = set(body.triggers) - set(feedback.TRIGGERS)
    if unknown:
        raise HTTPException(422, f"Unknown moment: {', '.join(sorted(unknown))}")
    business = await db.get(Business, current_user.business)
    feedback.write(business, body.model_dump())
    await db.flush()
    return await _settings_out(db, business)


@router.post("/template", response_model=TemplateOut)
async def submit_template(current_user: OwnerOrAdminDep, db: DbDep) -> TemplateOut:
    """Send the feedback template to Meta for approval (up to a day). Safe to press twice."""
    try:
        report = await required_templates.create_missing(db, current_user.business, "feedback")
    except template_service.WhatsAppNotReady as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return TemplateOut(created=report.created, already_there=report.already_there, failed=report.failed)


@router.get("/stats", response_model=StatsOut)
async def get_stats(current_user: OwnerOrAdminDep, db: DbDep, days: int = Query(default=30, ge=1, le=365)) -> StatsOut:
    return StatsOut(**await feedback.stats(db, current_user.business, days))


@router.get("/responses", response_model=list[ResponseOut])
async def list_responses(
    current_user: OwnerOrAdminDep, db: DbDep,
    show: str = Query(default="answered", pattern="^(answered|unhappy|happy|comments|all)$"),
    days: int = Query(default=30, ge=1, le=365), limit: int = Query(default=100, ge=1, le=300),
) -> list[ResponseOut]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    query = (
        select(FeedbackRequest, Customer.display_name).join(Customer, Customer.id == FeedbackRequest.customer_id)
        .where(FeedbackRequest.business_id == current_user.business, FeedbackRequest.created_at >= since)
    )
    if show == "answered":
        query = query.where(FeedbackRequest.status == "answered")
    elif show == "unhappy":
        query = query.where(FeedbackRequest.rating <= 2)
    elif show == "happy":
        query = query.where(FeedbackRequest.rating >= 4)
    elif show == "comments":
        query = query.where(FeedbackRequest.comment.is_not(None))
    rows = (await db.execute(
        query.order_by(FeedbackRequest.answered_at.desc().nullslast(), FeedbackRequest.created_at.desc()).limit(limit)
    )).all()
    return [
        ResponseOut(
            id=str(r.id), customer_id=str(r.customer_id), customer_name=name, trigger=r.trigger, about=r.about,
            channel=r.channel, status=r.status, rating=r.rating, comment=r.comment, answered_via=r.answered_via,
            answered_at=r.answered_at, created_at=r.created_at, note=r.note,
        )
        for r, name in rows
    ]
