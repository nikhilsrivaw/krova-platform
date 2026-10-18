"""
The platform operator's own panel (KROVA's staff, not a business). Read-mostly: it shows what is
happening across every business so the operator can check things without opening the database.

Who may use it: only staff who sign in at POST /admin/login (the emails and password hashes in the
server's ADMIN_PANEL_USERS). That is a separate sign-in from customer accounts; its token travels in the
X-Admin-Token header and is rejected on every other route. Without it every route here is 401.
It changes two things: a number request's status/notes (PATCH /admin/number-requests/{id}), and a
support ticket's status and the team's reply to it (PATCH /admin/support-tickets/{id}). Nothing else here writes.
"""

import uuid
from datetime import datetime, timedelta, timezone

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from services.api.dependencies import DbDep
from shared.auth import admin_panel
from shared.billing import plans
from shared.db.models import (
    Business,
    BusinessMember,
    BusinessRole,
    Channel,
    ChannelConnection,
    ConnectionStatus,
    NumberRequest,
    NumberRequestStatus,
    Payment,
    Subscription,
    SupportTicket,
    SupportTicketStatus,
    UsageEvent,
    UsageEventType,
    User,
    Wallet,
    WalletEntry,
)

router = APIRouter(prefix="/admin", tags=["admin"])


def _admin(x_admin_token: Annotated[str | None, Header()] = None) -> str:
    """The signed-in staff email; 401 when the admin token is missing, wrong or expired."""
    try:
        return admin_panel.verify_token(x_admin_token)
    except admin_panel.AdminAuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


AdminDep = Annotated[str, Depends(_admin)]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _month_start() -> datetime:
    return _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _rs(paise: int | None) -> str:
    return f"₹{(paise or 0) / 100:,.2f}"


async def _messages_by_business(db, ids: list[uuid.UUID] | None = None) -> dict[uuid.UUID, int]:
    """Customer messages this month (voice left out, same rule as the plan's message count)."""
    q = select(UsageEvent.business_id, func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
        UsageEvent.event_type == UsageEventType.message_processed,
        UsageEvent.channel != "voice",
        UsageEvent.occurred_at >= _month_start(),
    )
    if ids is not None:
        q = q.where(UsageEvent.business_id.in_(ids))
    return {bid: int(total) for bid, total in (await db.execute(q.group_by(UsageEvent.business_id))).all()}


# ── sign in ──────────────────────────────────────────────────────────────────

class LoginIn(BaseModel):
    email: str
    password: str


class LoginOut(BaseModel):
    token: str
    email: str


@router.post("/login", response_model=LoginOut)
async def login(body: LoginIn) -> LoginOut:
    try:
        token = admin_panel.login(body.email, body.password)
    except admin_panel.AdminAuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return LoginOut(token=token, email=body.email.strip().lower())


class AdminMe(BaseModel):
    email: str


@router.get("/me", response_model=AdminMe)
async def me(who: AdminDep) -> AdminMe:
    """The page asks this on load: 401 means show the sign-in form."""
    return AdminMe(email=who)


# ── overview ─────────────────────────────────────────────────────────────────

class Overview(BaseModel):
    businesses_total: int
    businesses_new_7d: int
    businesses_no_plan: int
    subs_active: int
    subs_past_due: int
    subs_suspended: int
    subs_cancelled: int
    mrr: str  # active + past-due plans, before GST
    wallet_total: str
    wallets_low: int
    wallets_rent_short: int
    number_requests_waiting: int
    support_tickets_open: int = 0
    number_requests_with_plivo: int
    payments_failed_7d: int
    payments_pending_old: int
    messages_this_month: int


@router.get("/overview", response_model=Overview)
async def overview(who: AdminDep, db: DbDep) -> Overview:
    now = _now()

    async def count(model, *where) -> int:
        return int((await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one())

    by_status = dict((await db.execute(select(Subscription.status, func.count()).group_by(Subscription.status))).all())
    live_amounts = (
        await db.execute(
            select(func.coalesce(func.sum(Subscription.amount_paise), 0)).where(
                Subscription.status.in_(("active", "past_due"))
            )
        )
    ).scalar_one()
    mrr_base = round(int(live_amounts) / (1 + plans.GST_PCT / 100))
    wallet_total = (await db.execute(select(func.coalesce(func.sum(Wallet.balance_paise), 0)))).scalar_one()
    with_plan = await count(Subscription)
    total = await count(Business)
    return Overview(
        businesses_total=total,
        businesses_new_7d=await count(Business, Business.created_at >= now - timedelta(days=7)),
        businesses_no_plan=max(total - with_plan, 0),
        subs_active=by_status.get("active", 0),
        subs_past_due=by_status.get("past_due", 0),
        subs_suspended=by_status.get("suspended", 0),
        subs_cancelled=by_status.get("cancelled", 0),
        mrr=_rs(mrr_base),
        wallet_total=_rs(int(wallet_total)),
        wallets_low=await count(Wallet, Wallet.balance_paise < plans.LOW_BALANCE_PAISE),
        wallets_rent_short=await count(Wallet, Wallet.rent_short_since.is_not(None)),
        number_requests_waiting=await count(NumberRequest, NumberRequest.status == NumberRequestStatus.requested),
        support_tickets_open=await count(SupportTicket, SupportTicket.status != SupportTicketStatus.resolved),
        number_requests_with_plivo=await count(
            NumberRequest, NumberRequest.status == NumberRequestStatus.submitted_to_plivo
        ),
        payments_failed_7d=await count(Payment, Payment.status == "failed", Payment.created_at >= now - timedelta(days=7)),
        payments_pending_old=await count(
            Payment, Payment.status == "pending", Payment.created_at < now - timedelta(hours=1)
        ),
        messages_this_month=sum((await _messages_by_business(db)).values()),
    )


# ── businesses ───────────────────────────────────────────────────────────────

class BusinessRow(BaseModel):
    id: str
    name: str
    vertical: str
    plan: str
    sub_status: str | None
    owner_name: str | None
    owner_email: str | None
    owner_phone: str | None
    created_at: str
    wallet: str | None
    voice_numbers: int
    messages_month: int
    is_active: bool


async def _owners(db, ids: list[uuid.UUID]) -> dict[uuid.UUID, User]:
    rows = await db.execute(
        select(BusinessMember.business_id, User)
        .join(User, User.id == BusinessMember.user_id)
        .where(BusinessMember.business_id.in_(ids), BusinessMember.role == BusinessRole.owner)
        .order_by(BusinessMember.created_at)
    )
    out: dict[uuid.UUID, User] = {}
    for bid, user in rows.all():
        out.setdefault(bid, user)
    return out


@router.get("/businesses", response_model=list[BusinessRow])
async def businesses(
    who: AdminDep, db: DbDep,
    q: str | None = Query(default=None, max_length=100), limit: int = Query(default=100, le=300),
) -> list[BusinessRow]:
    stmt = select(Business).order_by(Business.created_at.desc()).limit(limit)
    if q and q.strip():
        like = f"%{q.strip()}%"
        owner_ids = select(BusinessMember.business_id).join(User, User.id == BusinessMember.user_id).where(
            or_(User.email.ilike(like), User.phone.ilike(like))
        )
        stmt = stmt.where(or_(Business.name.ilike(like), Business.id.in_(owner_ids)))
    rows = (await db.execute(stmt)).scalars().all()
    ids = [b.id for b in rows]
    if not ids:
        return []
    subs = {s.business_id: s for s in (await db.execute(select(Subscription).where(Subscription.business_id.in_(ids)))).scalars().all()}
    wallets = {w.business_id: w for w in (await db.execute(select(Wallet).where(Wallet.business_id.in_(ids)))).scalars().all()}
    voice = dict(
        (await db.execute(
            select(ChannelConnection.business_id, func.count()).where(
                ChannelConnection.business_id.in_(ids), ChannelConnection.channel == Channel.voice,
                ChannelConnection.status == ConnectionStatus.active,
            ).group_by(ChannelConnection.business_id)
        )).all()
    )
    owners = await _owners(db, ids)
    messages = await _messages_by_business(db, ids)
    return [
        BusinessRow(
            id=str(b.id), name=b.name, vertical=b.vertical, plan=b.plan,
            sub_status=subs[b.id].status if b.id in subs else None,
            owner_name=owners[b.id].full_name if b.id in owners else None,
            owner_email=owners[b.id].email if b.id in owners else None,
            owner_phone=owners[b.id].phone if b.id in owners else None,
            created_at=b.created_at.isoformat(),
            wallet=_rs(wallets[b.id].balance_paise) if b.id in wallets else None,
            voice_numbers=int(voice.get(b.id, 0)), messages_month=messages.get(b.id, 0),
            is_active=b.is_active,
        )
        for b in rows
    ]


class PaymentRow(BaseModel):
    id: str
    business_id: str
    business_name: str | None
    purpose: str
    plan: str | None
    total: str
    status: str
    mode: str | None
    failure: str | None
    txnid: str
    created_at: str
    paid_at: str | None


def _payment_row(p: Payment, name: str | None) -> PaymentRow:
    return PaymentRow(
        id=str(p.id), business_id=str(p.business_id), business_name=name, purpose=p.purpose, plan=p.plan,
        total=_rs(p.total_paise), status=p.status, mode=p.mode, failure=p.failure, txnid=p.txnid,
        created_at=p.created_at.isoformat(), paid_at=_iso(p.paid_at),
    )


class WalletEntryRow(BaseModel):
    kind: str
    amount: str
    balance_after: str
    note: str | None
    created_at: str


class NumberRow(BaseModel):
    number: str
    label: str | None
    status: str


class BusinessDetail(BaseModel):
    business: BusinessRow
    settings_flags: dict
    team: list[dict]
    subscription: dict | None
    wallet_entries: list[WalletEntryRow]
    payments: list[PaymentRow]
    numbers: list[NumberRow]
    number_requests: list[dict]


@router.get("/businesses/{business_id}", response_model=BusinessDetail)
async def business_detail(business_id: uuid.UUID, who: AdminDep, db: DbDep) -> BusinessDetail:
    business = await db.get(Business, business_id)
    if business is None:
        raise HTTPException(404, "No such business")
    sub = await db.get(Subscription, business_id)
    wallet = await db.get(Wallet, business_id)
    owners = await _owners(db, [business_id])
    owner = owners.get(business_id)
    voice_rows = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == business_id, ChannelConnection.channel == Channel.voice
            )
        )
    ).scalars().all()
    members = (
        await db.execute(
            select(BusinessMember, User).join(User, User.id == BusinessMember.user_id)
            .where(BusinessMember.business_id == business_id).order_by(BusinessMember.created_at)
        )
    ).all()
    entries = (
        await db.execute(
            select(WalletEntry).where(WalletEntry.business_id == business_id).order_by(WalletEntry.created_at.desc()).limit(25)
        )
    ).scalars().all()
    pays = (
        await db.execute(
            select(Payment).where(Payment.business_id == business_id).order_by(Payment.created_at.desc()).limit(25)
        )
    ).scalars().all()
    reqs = (
        await db.execute(
            select(NumberRequest).where(NumberRequest.business_id == business_id).order_by(NumberRequest.created_at.desc())
        )
    ).scalars().all()
    msgs = (await _messages_by_business(db, [business_id])).get(business_id, 0)
    s = business.settings or {}
    return BusinessDetail(
        business=BusinessRow(
            id=str(business.id), name=business.name, vertical=business.vertical, plan=business.plan,
            sub_status=sub.status if sub else None,
            owner_name=owner.full_name if owner else None, owner_email=owner.email if owner else None,
            owner_phone=owner.phone if owner else None, created_at=business.created_at.isoformat(),
            wallet=_rs(wallet.balance_paise) if wallet else None,
            voice_numbers=sum(1 for v in voice_rows if v.status == ConnectionStatus.active),
            messages_month=msgs, is_active=business.is_active,
        ),
        settings_flags={
            "billing_required": bool(s.get("billing_required")),
            "autonomy": business.autonomy,
            "timezone": business.timezone,
        },
        team=[
            {"name": u.full_name, "email": u.email, "username": u.username, "role": m.role.value,
             "last_login": _iso(u.last_login_at), "active": u.is_active}
            for m, u in members
        ],
        subscription=None if sub is None else {
            "plan": sub.plan, "status": sub.status, "amount": _rs(sub.amount_paise),
            "current_period_end": sub.current_period_end.isoformat(), "next_charge_at": _iso(sub.next_charge_at),
            "attempts": sub.attempts, "last_failure": sub.last_failure, "cancel_at_period_end": sub.cancel_at_period_end,
        },
        wallet_entries=[
            WalletEntryRow(kind=e.kind, amount=_rs(e.amount_paise), balance_after=_rs(e.balance_after_paise),
                           note=e.note, created_at=e.created_at.isoformat())
            for e in entries
        ],
        payments=[_payment_row(p, business.name) for p in pays],
        numbers=[NumberRow(number=v.external_account_id, label=v.display_name, status=v.status.value) for v in voice_rows],
        number_requests=[
            {"id": str(r.id), "type": r.request_type.value, "status": r.status.value,
             "created_at": r.created_at.isoformat(), "provisioned_number": r.provisioned_number}
            for r in reqs
        ],
    )


# ── payments ─────────────────────────────────────────────────────────────────

@router.get("/payments", response_model=list[PaymentRow])
async def payments(
    who: AdminDep, db: DbDep,
    state: str | None = Query(default=None, pattern="^(pending|success|failed)$"), limit: int = Query(default=100, le=300),
) -> list[PaymentRow]:
    stmt = select(Payment, Business.name).join(Business, Business.id == Payment.business_id, isouter=True)
    if state:
        stmt = stmt.where(Payment.status == state)
    rows = (await db.execute(stmt.order_by(Payment.created_at.desc()).limit(limit))).all()
    return [_payment_row(p, name) for p, name in rows]


# ── 140 / 160 number requests, with who asked ────────────────────────────────

class AdminNumberRequest(BaseModel):
    id: str
    business_id: str
    business_name: str | None
    owner_name: str | None
    owner_email: str | None
    owner_phone: str | None
    request_type: str
    justification: str
    bfsi_declaration: bool
    status: str
    admin_notes: str | None
    provisioned_number: str | None
    created_at: str


@router.get("/number-requests", response_model=list[AdminNumberRequest])
async def number_requests(who: AdminDep, db: DbDep) -> list[AdminNumberRequest]:
    rows = (await db.execute(select(NumberRequest).order_by(NumberRequest.created_at.desc()).limit(300))).scalars().all()
    ids = list({r.business_id for r in rows})
    names = {}
    owners: dict[uuid.UUID, User] = {}
    if ids:
        names = {b.id: b.name for b in (await db.execute(select(Business).where(Business.id.in_(ids)))).scalars().all()}
        owners = await _owners(db, ids)
    return [
        AdminNumberRequest(
            id=str(r.id), business_id=str(r.business_id), business_name=names.get(r.business_id),
            owner_name=owners[r.business_id].full_name if r.business_id in owners else None,
            owner_email=owners[r.business_id].email if r.business_id in owners else None,
            owner_phone=owners[r.business_id].phone if r.business_id in owners else None,
            request_type=r.request_type.value, justification=r.justification, bfsi_declaration=r.bfsi_declaration,
            status=r.status.value, admin_notes=r.admin_notes, provisioned_number=r.provisioned_number,
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]


class NumberRequestUpdate(BaseModel):
    status: str | None = None
    admin_notes: str | None = None
    provisioned_number: str | None = None


@router.patch("/number-requests/{request_id}", response_model=AdminNumberRequest)
async def update_number_request(request_id: uuid.UUID, body: NumberRequestUpdate, who: AdminDep, db: DbDep) -> AdminNumberRequest:
    row = await db.get(NumberRequest, request_id)
    if row is None:
        raise HTTPException(404, "No such number request")
    if body.status is not None:
        try:
            row.status = NumberRequestStatus(body.status)
        except ValueError as exc:
            raise HTTPException(422, f"status must be one of {[s.value for s in NumberRequestStatus]}") from exc
    if body.admin_notes is not None:
        row.admin_notes = body.admin_notes
    if body.provisioned_number is not None:
        row.provisioned_number = body.provisioned_number
    await db.commit()
    business = await db.get(Business, row.business_id)
    owner = (await _owners(db, [row.business_id])).get(row.business_id)
    return AdminNumberRequest(
        id=str(row.id), business_id=str(row.business_id), business_name=business.name if business else None,
        owner_name=owner.full_name if owner else None, owner_email=owner.email if owner else None,
        owner_phone=owner.phone if owner else None, request_type=row.request_type.value,
        justification=row.justification, bfsi_declaration=row.bfsi_declaration, status=row.status.value,
        admin_notes=row.admin_notes, provisioned_number=row.provisioned_number, created_at=row.created_at.isoformat(),
    )


# ── support tickets (from the Help page) ─────────────────────────────────────

class AdminTicket(BaseModel):
    id: str
    number: str
    business_id: str
    business_name: str | None
    raised_by: str | None
    raised_by_contact: str | None
    subject: str
    category: str
    details: str
    page: str | None
    transcript: list[dict]
    status: str
    reply: str | None
    replied_at: str | None
    replied_by: str | None
    created_at: str


async def _ticket_rows(db, rows: list[SupportTicket]) -> list[AdminTicket]:
    from services.api.routers.help import ticket_number

    bids = list({t.business_id for t in rows})
    uids = list({t.created_by_user_id for t in rows if t.created_by_user_id})
    names = {b.id: b.name for b in (await db.execute(select(Business).where(Business.id.in_(bids)))).scalars().all()} if bids else {}
    users = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(uids)))).scalars().all()} if uids else {}
    out = []
    for t in rows:
        u = users.get(t.created_by_user_id)
        out.append(AdminTicket(
            id=str(t.id), number=ticket_number(t), business_id=str(t.business_id), business_name=names.get(t.business_id),
            raised_by=u.full_name if u else None,
            raised_by_contact=" · ".join(x for x in [u.email, u.phone] if x) if u else None,
            subject=t.subject, category=t.category.value, details=t.details, page=t.page,
            transcript=list(t.transcript or []), status=t.status.value, reply=t.reply,
            replied_at=_iso(t.replied_at), replied_by=t.replied_by, created_at=t.created_at.isoformat(),
        ))
    return out


@router.get("/support-tickets", response_model=list[AdminTicket])
async def support_tickets(who: AdminDep, db: DbDep, state: str | None = Query(default=None)) -> list[AdminTicket]:
    q = select(SupportTicket).order_by(SupportTicket.created_at.desc()).limit(300)
    if state == "open":
        q = q.where(SupportTicket.status != SupportTicketStatus.resolved)
    return await _ticket_rows(db, list((await db.execute(q)).scalars().all()))


class TicketUpdate(BaseModel):
    status: str | None = None
    reply: str | None = None


@router.patch("/support-tickets/{ticket_id}", response_model=AdminTicket)
async def update_support_ticket(ticket_id: uuid.UUID, body: TicketUpdate, who: AdminDep, db: DbDep) -> AdminTicket:
    """The business sees the reply and status on its Help page."""
    ticket = await db.get(SupportTicket, ticket_id)
    if ticket is None:
        raise HTTPException(404, "No such ticket")
    if body.status is not None:
        try:
            ticket.status = SupportTicketStatus(body.status)
        except ValueError as exc:
            raise HTTPException(422, f"status must be one of {[s.value for s in SupportTicketStatus]}") from exc
    if body.reply is not None and body.reply.strip() != (ticket.reply or ""):
        ticket.reply = body.reply.strip() or None
        ticket.replied_at = _now() if ticket.reply else None
        ticket.replied_by = who if ticket.reply else None
    await db.commit()
    await db.refresh(ticket)
    return (await _ticket_rows(db, [ticket]))[0]
