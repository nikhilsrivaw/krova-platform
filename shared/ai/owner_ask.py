"""
"Ask KROVA" for the owner: any question about their own business, answered from what KROVA really holds.

The old Ask KROVA was given only the Commitment Ledger, so "what happened last month?" or "what did Sharma ji say?"
got "not tracked" although KROVA had the messages, calls, bookings, orders, leads and payments all along.

Two model calls, both on the owner-ask model:
  1. plan   - a forced tool call reads the question and says which period, which customers (by the names or
              numbers the owner used) and which parts of the business it needs. Cheap: no business data is sent.
  2. answer - the facts for exactly that are gathered here, by plain SQL scoped to the business, and the model
              answers from them alone.

Never invent a fact: every figure in the answer comes from a query below. Anything not gathered is "not tracked",
and the facts say which period they cover so the answer can say it too. Private chats stay out, as everywhere
else the AI reads (Customer.is_private). Owner and admin only - the route is guarded, and they see every chat.
"""

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.care import ledger_queries
from shared.db.models import (
    Appointment,
    Business,
    BusinessMember,
    Call,
    Campaign,
    Commitment,
    CommitmentDirection,
    CommitmentStatus,
    Customer,
    CustomerDate,
    CustomerIdentity,
    CustomerIntelligence,
    CustomerNote,
    CustomerTag,
    Direction,
    Doctor,
    DraftStatus,
    Escalation,
    Insight,
    MessageDraft,
    Message,
    Order,
    PaymentLink,
    TagStatus,
    User,
    WalletEntry,
)
from shared.db.models.inbound_lead import InboundLead
from shared.db.models.queue import QueueEntry
from shared.db.models.quotation import Quotation, QuotationStatus

TOPICS = {
    "overview": "a summary of everything in the period (use for 'how is business', 'what happened')",
    "money": "money owed both ways, payments received, payment links",
    "conversations": "messages and chats: who wrote, who is waiting for a reply",
    "leads": "new enquiries and where they came from",
    "bookings": "appointments: booked, came, no-show, cancelled, upcoming",
    "queue": "walk-in tokens",
    "orders": "orders, COD, delivery status, order value",
    "quotations": "quotes sent, won, lost, gone quiet",
    "calls": "phone calls: answered, missed, what callers wanted",
    "escalations": "things the AI handed to a person, open or resolved",
    "signals": "complaints, churn risk, competitor mentions, praise",
    "team": "each team member's work",
    "campaigns": "WhatsApp campaigns sent",
    "approvals": "AI replies waiting, approved or rejected",
    "wallet": "the voice wallet balance and charges",
}

PLAN_TOOL = {
    "name": "look_up",
    "description": "Say what has to be looked up in the business's records to answer the owner's question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "period_from": {"type": "string", "description": "First day the question is about, YYYY-MM-DD."},
            "period_to": {"type": "string", "description": "Last day the question is about, YYYY-MM-DD (inclusive)."},
            "customers": {
                "type": "array", "items": {"type": "string"}, "maxItems": 3,
                "description": "Names or phone numbers of specific customers the owner asks about, exactly as written. "
                               "Empty if the question is not about particular people.",
            },
            "topics": {
                "type": "array", "items": {"type": "string", "enum": list(TOPICS)},
                "description": "Which parts of the business the answer needs.",
            },
        },
        "required": ["period_from", "period_to", "customers", "topics"],
    },
}

DEFAULT_DAYS = 30
MAX_DAYS = 366
LIST_LIMIT = 10
MESSAGE_LIMIT = 20


@dataclass
class Plan:
    start: date
    end: date
    customers: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)


def _rs(paise: int | None) -> str:
    return f"Rs {(paise or 0) / 100:,.0f}"


def _tz(business: Business | None) -> ZoneInfo:
    try:
        return ZoneInfo((business.timezone if business else None) or "Asia/Kolkata")
    except Exception:  # noqa: BLE001
        return ZoneInfo("Asia/Kolkata")


def _day(value, fallback: date) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return fallback


def plan_prompt(today: date) -> str:
    topics = "\n".join(f"- {k}: {v}" for k, v in TOPICS.items())
    return (
        "You decide what to look up in a business's own records so the owner's question can be answered. "
        f"Today is {today.isoformat()} ({today:%A}). Turn words like today, yesterday, this week, last week, "
        "this month, last month, pichle mahine, aaj, kal into exact dates. If no time is mentioned, use the last "
        f"{DEFAULT_DAYS} days. For a question about one customer, put their name or number in customers and pick "
        "the topics it touches (or leave topics to overview).\n\nTopics:\n" + topics
    )


def read_plan(raw: dict | None, today: date) -> Plan:
    """The model's plan, checked: real dates, a sane range, known topics. A missing plan reads as an overview."""
    raw = raw or {}
    default_start = today - timedelta(days=DEFAULT_DAYS - 1)
    start = _day(raw.get("period_from"), default_start)
    end = _day(raw.get("period_to"), today)
    if start > end:
        start, end = end, start
    end = min(end, today + timedelta(days=60))  # "upcoming bookings" may look ahead
    if (end - start).days > MAX_DAYS:
        start = end - timedelta(days=MAX_DAYS)
    topics = [t for t in (raw.get("topics") or []) if t in TOPICS]
    customers = [str(c).strip()[:60] for c in (raw.get("customers") or []) if str(c).strip()][:3]
    if not topics:
        topics = ["overview"]
    return Plan(start=start, end=end, customers=customers, topics=list(dict.fromkeys(topics)))


class Window:
    """The plan's days as UTC instants in the business's own time zone."""

    def __init__(self, plan: Plan, tz: ZoneInfo):
        self.start = datetime.combine(plan.start, time.min, tz).astimezone(timezone.utc)
        self.end = datetime.combine(plan.end + timedelta(days=1), time.min, tz).astimezone(timezone.utc)
        self.label = (
            f"{plan.start:%d %b %Y}" if plan.start == plan.end else f"{plan.start:%d %b %Y} to {plan.end:%d %b %Y}"
        )
        self.tz = tz

    def when(self, at: datetime | None) -> str:
        return at.astimezone(self.tz).strftime("%d %b %H:%M") if at else "-"


async def _count(db: AsyncSession, stmt) -> int:
    return int((await db.execute(stmt)).scalar() or 0)


# ── gathering ────────────────────────────────────────────────────────────────

async def gather(db: AsyncSession, business: Business, plan: Plan) -> str:
    """Everything the answer may use, as plain lines. Each section names its period."""
    bid = business.id
    w = Window(plan, _tz(business))
    now = datetime.now(timezone.utc)
    out: list[str] = [
        f"Business: {business.name}. Now: {w.when(now)} ({w.tz.key}). Period asked about: {w.label}.",
    ]
    out += await _ledger(db, bid, w)
    sections = {
        "overview": _overview, "money": _money, "conversations": _conversations, "leads": _leads,
        "bookings": _bookings, "queue": _queue, "orders": _orders, "quotations": _quotations, "calls": _calls,
        "escalations": _escalations, "signals": _signals, "team": _team, "campaigns": _campaigns,
        "approvals": _approvals, "wallet": _wallet,
    }
    for topic in plan.topics:
        out.append("")
        out += await sections[topic](db, bid, w)
    if plan.customers:
        out.append("")
        out += await _customers(db, bid, w, plan.customers)
    return "\n".join(out)


async def _ledger(db, bid, w: Window) -> list[str]:
    totals = await ledger_queries.totals(bid, db)
    they = await ledger_queries.top_open_commitments(bid, db, direction=CommitmentDirection.they_owe)
    we = await ledger_queries.top_open_commitments(bid, db, direction=CommitmentDirection.we_owe)
    lines = [
        "\nLEDGER RIGHT NOW (all open promises, not limited to the period):",
        f"- Customers owe the business {_rs(totals.owed_to_us_paise)}; overdue: {totals.overdue_they_owe_count} "
        f"promise(s), {_rs(totals.overdue_they_owe_paise)}.",
        f"- The business owes customers {_rs(totals.owed_by_us_paise)}; overdue promises to customers: "
        f"{totals.overdue_we_owe_count}.",
    ]
    if totals.unconfirmed_count:
        lines.append(f"- {totals.unconfirmed_count} promise(s) are unconfirmed (read from chat, not checked) - never state them as fact.")
    for title, rows in (("Most urgent owed TO the business", they), ("Most urgent promised BY the business", we)):
        if rows:
            lines.append(f"{title}:")
            lines += [
                f"  - {r.customer_name or 'a customer'}: {r.description} "
                f"({_rs(r.amount_paise) if r.amount_paise else 'no amount'}, due {w.when(r.due_at) if r.due_at else 'no date'})"
                for r in rows
            ]
    return lines


def _in(col, w: Window):
    return (col >= w.start) & (col < w.end)


async def _overview(db, bid, w: Window) -> list[str]:
    lines = [f"OVERVIEW, {w.label}:"]
    new_customers = await _count(db, select(func.count(Customer.id)).where(
        Customer.business_id == bid, _in(Customer.created_at, w)))
    inbound = await _count(db, select(func.count(Message.id)).where(
        Message.business_id == bid, Message.direction == Direction.inbound, _in(Message.occurred_at, w)))
    talkers = await _count(db, select(func.count(func.distinct(Message.customer_id))).where(
        Message.business_id == bid, Message.direction == Direction.inbound, _in(Message.occurred_at, w)))
    by_staff = await _count(db, select(func.count(Message.id)).where(
        Message.business_id == bid, Message.direction == Direction.outbound, Message.sent_by_user_id.is_not(None),
        _in(Message.occurred_at, w)))
    outbound = await _count(db, select(func.count(Message.id)).where(
        Message.business_id == bid, Message.direction == Direction.outbound, _in(Message.occurred_at, w)))
    lines.append(f"- New customers: {new_customers}. {talkers} customers wrote or spoke, {inbound} messages in.")
    lines.append(f"- Replies sent: {outbound} ({by_staff} by the team, {outbound - by_staff} by KROVA or automations).")
    leads = await _count(db, select(func.count(InboundLead.id)).where(
        InboundLead.business_id == bid, _in(InboundLead.received_at, w)))
    if leads:
        lines.append(f"- Leads received: {leads}.")
    booked = await _count(db, select(func.count(Appointment.id)).where(
        Appointment.business_id == bid, _in(Appointment.created_at, w)))
    if booked:
        lines.append(f"- Bookings made: {booked}.")
    orders = (await db.execute(select(func.count(Order.id), func.coalesce(func.sum(Order.total_paise), 0)).where(
        Order.business_id == bid, _in(Order.placed_at, w)))).one()
    if orders[0]:
        lines.append(f"- Orders placed: {orders[0]}, total value {_rs(orders[1])}.")
    received = await _received(db, bid, w)
    lines.append(f"- Money recorded as received on the Ledger: {_rs(received[0])} ({received[1]} payment(s)).")
    made = (await db.execute(select(Commitment.direction, func.count(Commitment.id)).where(
        Commitment.business_id == bid, _in(Commitment.created_at, w)).group_by(Commitment.direction))).all()
    made_map = {d.value if hasattr(d, "value") else d: n for d, n in made}
    lines.append(f"- New promises: {made_map.get('they_owe', 0)} owed to the business, {made_map.get('we_owe', 0)} by the business.")
    calls = await _count(db, select(func.count(Call.id)).where(Call.business_id == bid, _in(Call.started_at, w)))
    if calls:
        lines.append(f"- Calls: {calls}.")
    esc = await _count(db, select(func.count(Escalation.id)).where(Escalation.business_id == bid, _in(Escalation.created_at, w)))
    lines.append(f"- Handed to a person (escalations): {esc}.")
    sig = (await db.execute(select(Insight.kind, func.count(Insight.id)).where(
        Insight.business_id == bid, _in(Insight.created_at, w)).group_by(Insight.kind))).all()
    if sig:
        lines.append("- Signals: " + ", ".join(f"{k} {n}" for k, n in sig) + ".")
    lines.append("- Not tracked by KROVA: revenue outside these payments and orders, expenses, profit.")
    return lines


async def _received(db, bid, w: Window) -> tuple[int, int]:
    """Money written to promises in the period - from payment_log, one entry per receipt."""
    rows = (await db.execute(select(Commitment.payment_log).where(
        Commitment.business_id == bid, Commitment.updated_at >= w.start, Commitment.amount_received_paise > 0,
    ))).scalars().all()
    total = count = 0
    for log in rows:
        for entry in log or []:
            try:
                at = datetime.fromisoformat(str(entry.get("at")))
            except (TypeError, ValueError):
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            if w.start <= at < w.end:
                total += int(entry.get("amount_paise") or 0)
                count += 1
    return total, count


async def _money(db, bid, w: Window) -> list[str]:
    total, n = await _received(db, bid, w)
    lines = [f"MONEY, {w.label}:", f"- Recorded as received: {_rs(total)} in {n} payment(s)."]
    paid = (await db.execute(select(func.count(PaymentLink.id), func.coalesce(func.sum(PaymentLink.amount_paid_paise), 0)).where(
        PaymentLink.business_id == bid, _in(PaymentLink.paid_at, w)))).one()
    sent = await _count(db, select(func.count(PaymentLink.id)).where(PaymentLink.business_id == bid, _in(PaymentLink.created_at, w)))
    lines.append(f"- Payment links sent: {sent}; paid: {paid[0]} for {_rs(paid[1])}.")
    for status in (CommitmentStatus.met, CommitmentStatus.missed):
        k = await _count(db, select(func.count(Commitment.id)).where(
            Commitment.business_id == bid, Commitment.status == status, _in(Commitment.resolved_at, w)))
        lines.append(f"- Promises marked {status.value}: {k}.")
    rows = (await db.execute(
        select(Commitment, Customer.display_name).join(Customer, Customer.id == Commitment.customer_id).where(
            Commitment.business_id == bid, Commitment.status == CommitmentStatus.open,
            Commitment.direction == CommitmentDirection.they_owe, Commitment.due_at < datetime.now(timezone.utc),
            Customer.is_private == False,  # noqa: E712
        ).order_by(Commitment.due_at).limit(LIST_LIMIT)
    )).all()
    if rows:
        lines.append("Overdue, owed to the business (oldest first):")
        lines += [f"  - {name or 'a customer'}: {c.description} - {_rs(c.outstanding_paise)} "
                  f"due {w.when(c.due_at)}" for c, name in rows]
    return lines


async def _conversations(db, bid, w: Window) -> list[str]:
    lines = [f"CONVERSATIONS, {w.label}:"]
    latest = (await db.execute(
        select(Message.customer_id, func.max(Message.occurred_at)).where(
            Message.business_id == bid, _in(Message.occurred_at, w)).group_by(Message.customer_id)
        .order_by(func.max(Message.occurred_at).desc()).limit(LIST_LIMIT)
    )).all()
    for customer_id, last_at in latest:
        customer = await db.get(Customer, customer_id)
        if customer is None or customer.is_private:
            continue
        last = (await db.execute(select(Message).where(Message.customer_id == customer_id)
                                 .order_by(Message.occurred_at.desc()).limit(1))).scalar_one_or_none()
        waiting = " - WAITING for a reply" if last is not None and last.direction == Direction.inbound else ""
        said = (last.content or "").replace("\n", " ")[:120] if last else ""
        lines.append(f"  - {customer.display_name or 'unnamed'} ({w.when(last_at)}, {last.channel.value if last else ''}): \"{said}\"{waiting}")
    if len(lines) == 1:
        lines.append("- No conversations in this period.")
    return lines


async def _leads(db, bid, w: Window) -> list[str]:
    by_source = (await db.execute(select(InboundLead.source, func.count(InboundLead.id)).where(
        InboundLead.business_id == bid, _in(InboundLead.received_at, w)).group_by(InboundLead.source))).all()
    lines = [f"LEADS, {w.label}: " + (", ".join(f"{s} {n}" for s, n in by_source) if by_source else "none")]
    rows = (await db.execute(select(InboundLead).where(InboundLead.business_id == bid, _in(InboundLead.received_at, w))
                             .order_by(InboundLead.received_at.desc()).limit(LIST_LIMIT))).scalars().all()
    lines += [f"  - {r.name or r.phone or 'unknown'} via {r.source}, {w.when(r.received_at)}, status {r.status}"
              + (f": {r.query[:100]}" if r.query else "") for r in rows]
    return lines


async def _bookings(db, bid, w: Window) -> list[str]:
    by_status = (await db.execute(select(Appointment.status, func.count(Appointment.id)).where(
        Appointment.business_id == bid, _in(Appointment.starts_at, w)).group_by(Appointment.status))).all()
    lines = [f"BOOKINGS with a time in {w.label}: " + (", ".join(f"{s.value} {n}" for s, n in by_status) if by_status else "none")]
    rows = (await db.execute(
        select(Appointment, Customer.display_name, Doctor.name)
        .join(Customer, Customer.id == Appointment.customer_id).outerjoin(Doctor, Doctor.id == Appointment.doctor_id)
        .where(Appointment.business_id == bid, _in(Appointment.starts_at, w)).order_by(Appointment.starts_at).limit(LIST_LIMIT * 2)
    )).all()
    lines += [f"  - {w.when(a.starts_at)}: {name or 'a customer'}" + (f" with {doc}" if doc else "") + f" - {a.status.value}"
              for a, name, doc in rows]
    return lines


async def _queue(db, bid, w: Window) -> list[str]:
    rows = (await db.execute(select(QueueEntry.status, func.count(QueueEntry.id)).where(
        QueueEntry.business_id == bid, _in(QueueEntry.created_at, w)).group_by(QueueEntry.status))).all()
    return [f"WALK-IN TOKENS, {w.label}: " + (", ".join(f"{s.value} {n}" for s, n in rows) if rows else "none")]


async def _orders(db, bid, w: Window) -> list[str]:
    by_status = (await db.execute(select(Order.status, func.count(Order.id), func.coalesce(func.sum(Order.total_paise), 0)).where(
        Order.business_id == bid, _in(Order.placed_at, w)).group_by(Order.status))).all()
    lines = [f"ORDERS placed in {w.label}: " + (", ".join(f"{s.value} {n} ({_rs(v)})" for s, n, v in by_status) if by_status else "none")]
    cod = await _count(db, select(func.count(Order.id)).where(Order.business_id == bid, Order.is_cod == True, _in(Order.placed_at, w)))  # noqa: E712
    if cod:
        lines.append(f"- Cash on delivery: {cod}.")
    rows = (await db.execute(select(Order, Customer.display_name).outerjoin(Customer, Customer.id == Order.customer_id)
                             .where(Order.business_id == bid, _in(Order.placed_at, w)).order_by(Order.placed_at.desc()).limit(LIST_LIMIT))).all()
    lines += [f"  - {o.order_number or o.external_order_id}: {name or 'a customer'}, {_rs(o.total_paise)}, {o.status.value}"
              + (" COD" if o.is_cod else "") for o, name in rows]
    return lines


async def _quotations(db, bid, w: Window) -> list[str]:
    by_status = (await db.execute(select(Quotation.status, func.count(Quotation.id), func.coalesce(func.sum(Quotation.total_paise), 0)).where(
        Quotation.business_id == bid, _in(Quotation.created_at, w)).group_by(Quotation.status))).all()
    lines = [f"QUOTATIONS made in {w.label}: " + (", ".join(f"{s.value} {n} ({_rs(v)})" for s, n, v in by_status) if by_status else "none")]
    rows = (await db.execute(select(Quotation, Customer.display_name).join(Customer, Customer.id == Quotation.customer_id).where(
        Quotation.business_id == bid, Quotation.status.in_((QuotationStatus.sent, QuotationStatus.negotiating))).order_by(Quotation.sent_at).limit(LIST_LIMIT))).all()
    if rows:
        lines.append("Open quotes, oldest first:")
        lines += [f"  - {name}: {q.reference or ''} {_rs(q.total_paise)}, sent {w.when(q.sent_at)}, followed up {q.follow_up_count} time(s)"
                  for q, name in rows]
    return lines


async def _calls(db, bid, w: Window) -> list[str]:
    total = (await db.execute(select(func.count(Call.id), func.coalesce(func.sum(Call.duration_seconds), 0),
                                     func.coalesce(func.sum(Call.cost_paise), 0)).where(Call.business_id == bid, _in(Call.started_at, w)))).one()
    esc = await _count(db, select(func.count(Call.id)).where(Call.business_id == bid, Call.escalated == True, _in(Call.started_at, w)))  # noqa: E712
    lines = [f"CALLS, {w.label}: {total[0]} call(s), {int(total[1]) // 60} minutes, {esc} handed to a person, call cost {_rs(total[2])}."]
    rows = (await db.execute(select(Call, Customer.display_name).outerjoin(Customer, Customer.id == Call.customer_id).where(
        Call.business_id == bid, _in(Call.started_at, w)).order_by(Call.started_at.desc()).limit(LIST_LIMIT))).all()
    lines += [f"  - {w.when(c.started_at)} {c.direction.value} {name or 'unknown caller'}: {c.outcome or ''} "
              f"{('- ' + c.summary[:140]) if c.summary else ''}".rstrip() for c, name in rows]
    return lines


async def _escalations(db, bid, w: Window) -> list[str]:
    open_now = await _count(db, select(func.count(Escalation.id)).where(Escalation.business_id == bid, Escalation.resolved_at.is_(None)))
    made = await _count(db, select(func.count(Escalation.id)).where(Escalation.business_id == bid, _in(Escalation.created_at, w)))
    lines = [f"ESCALATIONS: {made} in {w.label}; {open_now} open right now."]
    rows = (await db.execute(select(Escalation, Customer.display_name).outerjoin(Customer, Customer.id == Escalation.customer_id).where(
        Escalation.business_id == bid, Escalation.resolved_at.is_(None)).order_by(Escalation.created_at.desc()).limit(LIST_LIMIT))).all()
    lines += [f"  - open since {w.when(e.created_at)}: {name or e.caller_phone or 'a customer'} - {e.reason[:120]}" for e, name in rows]
    return lines


async def _signals(db, bid, w: Window) -> list[str]:
    rows = (await db.execute(select(Insight, Customer.display_name).outerjoin(Customer, Customer.id == Insight.customer_id).where(
        Insight.business_id == bid, _in(Insight.created_at, w), Insight.dismissed_at.is_(None))
        .order_by(Insight.created_at.desc()).limit(LIST_LIMIT * 2))).all()
    lines = [f"SIGNALS, {w.label}: {len(rows)} shown"]
    lines += [f"  - {i.kind} ({i.severity}) {name or ''}: {i.title}" for i, name in rows]
    return lines


async def _team(db, bid, w: Window) -> list[str]:
    members = (await db.execute(select(BusinessMember, User).join(User, User.id == BusinessMember.user_id)
                                .where(BusinessMember.business_id == bid))).all()
    lines = [f"TEAM, {w.label}:"]
    for member, user in members:
        uid = user.id
        sent = await _count(db, select(func.count(Message.id)).where(Message.business_id == bid, Message.sent_by_user_id == uid, _in(Message.occurred_at, w)))
        reviewed = await _count(db, select(func.count(MessageDraft.id)).where(MessageDraft.business_id == bid, MessageDraft.reviewed_by_user_id == uid, _in(MessageDraft.reviewed_at, w)))
        acked = await _count(db, select(func.count(Escalation.id)).where(Escalation.business_id == bid, Escalation.acknowledged_by_user_id == uid, _in(Escalation.acknowledged_at, w)))
        calls = await _count(db, select(func.count(Call.id)).where(Call.business_id == bid, Call.handled_by_user_id == uid, _in(Call.started_at, w)))
        chats = await _count(db, select(func.count(Customer.id)).where(Customer.business_id == bid, Customer.assigned_to_user_id == uid))
        role = member.role.value if hasattr(member.role, "value") else member.role
        lines.append(
            f"  - {user.full_name or user.username or user.email} ({role}{', away' if not member.available else ''}): "
            f"{sent} messages sent, {reviewed} AI replies reviewed, {acked} escalations taken, {calls} calls taken, "
            f"owns {chats} chats, last sign-in {w.when(user.last_login_at)}"
        )
    return lines


async def _campaigns(db, bid, w: Window) -> list[str]:
    rows = (await db.execute(select(Campaign).where(Campaign.business_id == bid, _in(Campaign.created_at, w))
                             .order_by(Campaign.created_at.desc()).limit(LIST_LIMIT))).scalars().all()
    lines = [f"WHATSAPP CAMPAIGNS created in {w.label}: {len(rows)}"]
    lines += [f"  - {c.name}: {c.status.value}, {c.sent_count} sent, {c.failed_count} failed, {c.skipped_count} skipped" for c in rows]
    return lines


async def _approvals(db, bid, w: Window) -> list[str]:
    rows = (await db.execute(select(MessageDraft.status, func.count(MessageDraft.id)).where(
        MessageDraft.business_id == bid, _in(MessageDraft.created_at, w)).group_by(MessageDraft.status))).all()
    waiting = await _count(db, select(func.count(MessageDraft.id)).where(MessageDraft.business_id == bid, MessageDraft.status == DraftStatus.pending))
    return [f"AI REPLIES drafted in {w.label}: " + (", ".join(f"{s.value} {n}" for s, n in rows) if rows else "none")
            + f". Waiting for approval right now: {waiting}."]


async def _wallet(db, bid, w: Window) -> list[str]:
    from shared.billing import wallet

    balance = await wallet.balance(db, bid)
    spent = await _count(db, select(func.coalesce(func.sum(WalletEntry.amount_paise), 0)).where(
        WalletEntry.business_id == bid, WalletEntry.amount_paise < 0, _in(WalletEntry.created_at, w)))
    return [f"VOICE WALLET: balance {_rs(balance) if balance is not None else 'no wallet yet'}; "
            f"spent in {w.label}: {_rs(-spent)}."]


# ── one customer ─────────────────────────────────────────────────────────────

async def find_customers(db, bid, term: str) -> list[Customer]:
    digits = re.sub(r"\D", "", term)
    found: dict[uuid.UUID, Customer] = {}
    if len(digits) >= 6:
        rows = (await db.execute(select(Customer).join(CustomerIdentity, CustomerIdentity.customer_id == Customer.id).where(
            Customer.business_id == bid, CustomerIdentity.value.like(f"%{digits[-10:]}%")).limit(3))).scalars().all()
        found.update({c.id: c for c in rows})
    words = [x for x in re.split(r"\s+", re.sub(r"\b(ji|sir|madam|bhai|didi|bhaiya|mr|mrs|ms|dr)\b", " ", term.lower())) if len(x) >= 2]
    if words and not found:
        cond = [Customer.display_name.ilike(f"%{x}%") for x in words]
        rows = (await db.execute(select(Customer).where(Customer.business_id == bid, *cond)
                                 .order_by(Customer.last_contact_at.desc().nullslast()).limit(3))).scalars().all()
        if not rows:
            rows = (await db.execute(select(Customer).where(Customer.business_id == bid, or_(*cond))
                                     .order_by(Customer.last_contact_at.desc().nullslast()).limit(3))).scalars().all()
        found.update({c.id: c for c in rows})
    return [c for c in found.values() if not c.is_private]


async def _customers(db, bid, w: Window, terms: list[str]) -> list[str]:
    lines: list[str] = []
    seen: set[uuid.UUID] = set()
    for term in terms:
        matches = await find_customers(db, bid, term)
        if not matches:
            lines.append(f"CUSTOMER \"{term}\": no customer with that name or number (private chats are never shown).")
            continue
        if len(matches) > 1:
            lines.append(f"CUSTOMER \"{term}\": {len(matches)} customers match - " + ", ".join(c.display_name or "unnamed" for c in matches)
                         + ". Details of each follow; ask the owner which one if it matters.")
        for customer in matches:
            if customer.id not in seen:
                seen.add(customer.id)
                lines += await customer_facts(db, bid, customer, w)
    return lines


async def customer_facts(db, bid, c: Customer, w: Window) -> list[str]:
    lines = [f"CUSTOMER {c.display_name or 'unnamed'}:"]
    phones = (await db.execute(select(CustomerIdentity.kind, CustomerIdentity.value).where(CustomerIdentity.customer_id == c.id))).all()
    owner = None
    if c.assigned_to_user_id:
        u = await db.get(User, c.assigned_to_user_id)
        owner = u.full_name or u.username if u else None
    lines.append(
        f"- Contact: {', '.join(f'{k.value if hasattr(k, 'value') else k} {v}' for k, v in phones) or 'none'}. "
        f"First seen {w.when(c.first_seen_at or c.created_at)}, last contact {w.when(c.last_contact_at)}."
        + (f" Stage: {c.stage}." if c.stage else "") + (f" Price tier: {c.price_tier}." if c.price_tier else "")
        + (f" Deal value {_rs(c.deal_value_paise)}." if c.deal_value_paise else "")
        + (f" Handled by {owner}." if owner else "") + (f" Came from the ad \"{c.ctwa_headline}\"." if c.ctwa_headline else "")
    )
    intel = await db.get(CustomerIntelligence, c.id)
    if intel and intel.summary:
        lines.append(f"- KROVA's profile (health {intel.health_score}/100): {intel.summary[:600]}")
    tags = (await db.execute(select(CustomerTag.label).where(CustomerTag.customer_id == c.id, CustomerTag.status == TagStatus.confirmed))).scalars().all()
    if tags:
        lines.append("- Tags: " + ", ".join(tags))
    dates = (await db.execute(select(CustomerDate).where(CustomerDate.customer_id == c.id))).scalars().all()
    if dates:
        lines.append("- Key dates: " + "; ".join(f"{d.label} {d.date:%d %b %Y}" for d in dates))
    promises = (await db.execute(select(Commitment).where(Commitment.customer_id == c.id).order_by(
        Commitment.status != CommitmentStatus.open, Commitment.due_at.asc().nullslast()).limit(12))).scalars().all()
    if promises:
        lines.append("- Promises:")
        for p in promises:
            who = "owes the business" if p.direction == CommitmentDirection.they_owe else "business promised"
            amount = f", {_rs(p.amount_paise)}" if p.amount_paise else ""
            got = f", received {_rs(p.amount_received_paise)}" if p.amount_received_paise else ""
            lines.append(f"  - [{p.status.value}] {who}: {p.description}{amount}{got}, due {w.when(p.due_at)}"
                         + (f' (they said: "{p.source_quote[:100]}")' if p.source_quote else ""))
    appts = (await db.execute(select(Appointment).where(Appointment.customer_id == c.id).order_by(Appointment.starts_at.desc()).limit(5))).scalars().all()
    if appts:
        lines.append("- Bookings: " + "; ".join(f"{w.when(a.starts_at)} {a.status.value}" for a in appts))
    orders = (await db.execute(select(Order).where(Order.customer_id == c.id).order_by(Order.placed_at.desc()).limit(5))).scalars().all()
    if orders:
        lines.append("- Orders: " + "; ".join(f"{o.order_number or o.external_order_id} {_rs(o.total_paise)} {o.status.value}" for o in orders))
    quotes = (await db.execute(select(Quotation).where(Quotation.customer_id == c.id).order_by(Quotation.created_at.desc()).limit(5))).scalars().all()
    if quotes:
        lines.append("- Quotes: " + "; ".join(f"{q.reference or ''} {_rs(q.total_paise)} {q.status.value}" for q in quotes))
    calls = (await db.execute(select(Call).where(Call.customer_id == c.id).order_by(Call.started_at.desc()).limit(5))).scalars().all()
    if calls:
        lines.append("- Calls: " + "; ".join(f"{w.when(x.started_at)} {x.outcome or ''} {(x.summary or '')[:100]}".strip() for x in calls))
    notes = (await db.execute(select(CustomerNote.body).where(CustomerNote.customer_id == c.id).order_by(CustomerNote.created_at.desc()).limit(5))).scalars().all()
    if notes:
        lines.append("- Team notes: " + " | ".join(n[:150] for n in notes))
    open_esc = (await db.execute(select(Escalation.reason).where(Escalation.customer_id == c.id, Escalation.resolved_at.is_(None)))).scalars().all()
    if open_esc:
        lines.append("- Open escalations: " + " | ".join(r[:120] for r in open_esc))
    messages = (await db.execute(select(Message).where(Message.customer_id == c.id).order_by(Message.occurred_at.desc()).limit(MESSAGE_LIMIT))).scalars().all()
    if messages:
        lines.append(f"- Last {len(messages)} messages, oldest first:")
        for m in reversed(messages):
            who = "customer" if m.direction == Direction.inbound else ("team" if m.sent_by_user_id else "business/KROVA")
            lines.append(f"  - {w.when(m.occurred_at)} {m.channel.value} {who}: {(m.content or '[media]').replace(chr(10), ' ')[:300]}")
    return lines


ANSWER_RULES = (
    "You are KROVA, answering the business owner (or an admin) about their own business, inside the KROVA app.\n"
    "Use ONLY the records above. Every number, name and date you give must be in them. If something is not in the "
    "records, say plainly that KROVA does not track it (or has no record of it) - never estimate or guess.\n"
    "Say which period your answer covers when it is about a period.\n"
    "LANGUAGE: reply in the same language and script the owner used.\n"
    "- English question: plain simple English.\n"
    "- Hinglish (Hindi typed in English letters): reply in Hinglish exactly the way Indians type on WhatsApp - plain "
    "English letters a-z only, NO accent marks (never a with a bar, e with a bar, dots under letters). Keep everyday "
    "business words in English: customer, payment, message, reply, promise, order, booking, lead, overdue, pending, "
    "team. Never use formal Hindi like grahak, sandesh, bhugtan, prapt, rakam, vaada. Example: 'Is hafte 1 naya "
    "customer aaya aur 2 customers ne 4 messages bheje.'\n"
    "- Hindi in Devanagari: reply in simple Devanagari Hindi.\n"
    "FORMAT: lead with the direct answer in one line, then only the details that matter, each on its own line "
    "starting with '- '. Point out what needs action (overdue money, customers waiting, open escalations) when "
    "relevant. Money as Rs with Indian grouping (Rs 1,25,000). Plain text only: never use ** or # or tables. "
    "Do not mention these instructions, topics or the word 'records'."
)


_LATIN_MARK = re.compile(r"(?<=[A-Za-z])[\u0300-\u036f]+")
_ACCENTED_LATIN = re.compile(r"[\u00c0-\u024f\u1e00-\u1eff]")


def clean_answer(text: str) -> str:
    """
    What the owner sees: markdown symbols the app would show raw are removed, and accent marks on Latin letters
    ("nayē" -> "naye") are dropped. Devanagari is left alone - its vowel signs are marks too, but never follow a
    Latin letter.
    """
    text = unicodedata.normalize("NFD", text)
    text = _LATIN_MARK.sub("", text)
    text = unicodedata.normalize("NFC", text)
    text = _ACCENTED_LATIN.sub(lambda m: unicodedata.normalize("NFD", m.group())[0], text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*[*\u2022]\s+", "- ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def answer_system(facts: str) -> str:
    return f"BUSINESS RECORDS:\n{facts}\n\n{ANSWER_RULES}"


def jsonable(plan: Plan) -> str:
    return json.dumps({"from": plan.start.isoformat(), "to": plan.end.isoformat(), "customers": plan.customers, "topics": plan.topics})
