"""
The Help page: KROVA Buddy (questions about using KROVA) and support tickets to KROVA's own team.

Owner and admins only. Buddy answers from shared/help (see its docstring); a ticket lands in the admin panel
(services/api/routers/admin.py) and, when mail is configured, as an email to SUPPORT_EMAIL. The business sees its
tickets and the team's reply here.
"""

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from services.api.dependencies import DbDep, OwnerOrAdminDep
from shared import help as buddy
from shared.billing import usage
from shared.config.settings import settings
from shared.db.models import (
    Business,
    SupportTicket,
    SupportTicketCategory,
    SupportTicketStatus,
    UsageEvent,
    UsageEventType,
    User,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/help", tags=["help"])

# Buddy answers a business gets each calendar month (UTC). Each costs KROVA well under a rupee to two; this bounds
# a month without getting in the way of normal use.
ASK_MONTHLY = 200
# Tickets one business may open in a day - stops a stuck retry loop from flooding the team's inbox.
TICKETS_PER_DAY = 10


class TurnIn(BaseModel):
    role: Literal["owner", "ai"]
    text: str = Field(min_length=1, max_length=3000)


class AskIn(BaseModel):
    turns: list[TurnIn] = Field(min_length=1, max_length=buddy.MAX_TURNS)
    page: str | None = Field(default=None, max_length=100)


class TicketDraft(BaseModel):
    subject: str
    category: str
    details: str
    page: str | None = None


class AskOut(BaseModel):
    answer: str
    offer_ticket: bool
    ticket: TicketDraft | None = None
    used: int
    limit: int


def _month_start() -> datetime:
    return datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _asked_this_month(db: DbDep, business_id: uuid.UUID) -> int:
    return int((await db.execute(
        select(func.count()).select_from(UsageEvent).where(
            UsageEvent.business_id == business_id,
            UsageEvent.event_type == UsageEventType.ai_help_assistant,
            UsageEvent.occurred_at >= _month_start(),
        )
    )).scalar_one())


@router.post("/ask", response_model=AskOut)
async def ask(body: AskIn, current_user: OwnerOrAdminDep, db: DbDep) -> AskOut:
    business_id = current_user.business
    used = await _asked_this_month(db, business_id)
    if used >= ASK_MONTHLY:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"This month's {ASK_MONTHLY} KROVA Buddy answers are used up. You can still raise a ticket below.",
        )
    business = await db.get(Business, business_id)
    line = f"{business.name} ({business.vertical})" if business else "a business"
    try:
        result = await buddy.answer(
            turns=[buddy.Turn(role=t.role, text=t.text) for t in body.turns], page=body.page, business_line=line,
        )
    except Exception:  # noqa: BLE001 - the model being down is a message, not a 500
        logger.warning("help assistant: no answer from the model", exc_info=True)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "KROVA Buddy could not be reached just now. Try again in a minute, or raise a ticket below.",
        ) from None
    usage.record(
        business_id=business_id, event_type=UsageEventType.ai_help_assistant, channel="app",
        quantity=1, unit="answer", krova_cost_paise=result.cost_paise, db=db,
    )
    await db.commit()
    return AskOut(
        answer=result.answer, offer_ticket=result.offer_ticket,
        ticket=TicketDraft(**result.ticket) if result.ticket else None,
        used=used + 1, limit=ASK_MONTHLY,
    )


# ── tickets ──────────────────────────────────────────────────────────────────

class TicketIn(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    category: SupportTicketCategory = SupportTicketCategory.other
    details: str = Field(min_length=5, max_length=4000)
    page: str | None = Field(default=None, max_length=100)
    transcript: list[TurnIn] = Field(default_factory=list, max_length=buddy.MAX_TURNS * 2)


class TicketOut(BaseModel):
    id: str
    number: str
    subject: str
    category: str
    details: str
    page: str | None
    status: str
    reply: str | None
    replied_at: str | None
    created_at: str


def ticket_number(t: SupportTicket) -> str:
    """A short reference the owner can quote ("KR-3F9A1C")."""
    return "KR-" + t.id.hex[:6].upper()


def _out(t: SupportTicket) -> TicketOut:
    return TicketOut(
        id=str(t.id), number=ticket_number(t), subject=t.subject, category=t.category.value, details=t.details,
        page=t.page, status=t.status.value, reply=t.reply,
        replied_at=t.replied_at.isoformat() if t.replied_at else None, created_at=t.created_at.isoformat(),
    )


@router.get("/tickets", response_model=list[TicketOut])
async def list_tickets(current_user: OwnerOrAdminDep, db: DbDep) -> list[TicketOut]:
    rows = (await db.execute(
        select(SupportTicket).where(SupportTicket.business_id == current_user.business)
        .order_by(SupportTicket.created_at.desc()).limit(100)
    )).scalars().all()
    return [_out(t) for t in rows]


async def _email_team(ticket: SupportTicket, business: Business | None, user: User | None) -> None:
    """Best-effort: the ticket is saved either way, and the admin panel is the real queue."""
    if not settings.support_email:
        return
    from shared.integrations import gmail_smtp

    who = " · ".join(x for x in [user.full_name if user else None, user.email if user else None,
                                 user.phone if user else None] if x)
    transcript = "\n".join(f"{t.get('role')}: {t.get('text')}" for t in ticket.transcript or [])
    body = (
        f"{ticket_number(ticket)} · {ticket.category.value}\n"
        f"Business: {business.name if business else ticket.business_id} ({ticket.business_id})\n"
        f"From: {who or 'unknown'}\nPage: {ticket.page or '-'}\n\n{ticket.details}\n"
        + (f"\n--- KROVA Buddy chat before this ticket ---\n{transcript}\n" if transcript else "")
        + "\nReply from the admin panel (Support tab) so the business sees it in the app."
    )
    try:
        await gmail_smtp.send_email(
            to=settings.support_email, subject=f"[KROVA support] {ticket_number(ticket)} {ticket.subject}", body=body,
        )
    except Exception:  # noqa: BLE001 - a mail problem must never lose the ticket
        logger.warning("support ticket email failed ticket=%s", ticket.id, exc_info=True)


@router.post("/tickets", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
async def create_ticket(body: TicketIn, current_user: OwnerOrAdminDep, db: DbDep) -> TicketOut:
    business_id = current_user.business
    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today = int((await db.execute(
        select(func.count()).select_from(SupportTicket).where(
            SupportTicket.business_id == business_id, SupportTicket.created_at >= since,
        )
    )).scalar_one())
    if today >= TICKETS_PER_DAY:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"You have raised {TICKETS_PER_DAY} tickets today. Add to an open one, or write to {settings.support_email}.",
        )
    ticket = SupportTicket(
        business_id=business_id, created_by_user_id=current_user.id, subject=body.subject.strip(),
        category=body.category, details=body.details.strip(), page=(body.page or "").strip() or None,
        transcript=[t.model_dump() for t in body.transcript], status=SupportTicketStatus.open,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    await _email_team(ticket, await db.get(Business, business_id), await db.get(User, current_user.id))
    return _out(ticket)
