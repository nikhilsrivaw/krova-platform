"""
The one-glance briefing a team member sees before they speak to a customer.

When a call is put through, a chat is claimed, or a lead is about to be rung, the person
should not have to scroll back through a thread to learn who this is and what is going on.
Everything here is read straight from what KROVA already holds - no AI call, so it costs
nothing and is ready instantly:

  who         name, language, tags, stage, which team member owns them
  why         the lead enquiry that brought them (Justdial, IndiaMART ...), and the last thing
              they said
  history     the AI's summary of them, the last few messages, the last call, notes
  open items  promises either side has made and not kept, and what is owed
  next        an upcoming booking, any open escalation

A customer the owner marked private is reduced to a name - the same promise the rest of the
product makes about private threads.
"""

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.care.commitment_payments import OUTSTANDING
from shared.db.models import (
    Appointment,
    AppointmentStatus,
    Call,
    Commitment,
    CommitmentDirection,
    CommitmentStatus,
    Customer,
    CustomerIdentity,
    CustomerIntelligence,
    CustomerNote,
    CustomerTag,
    Direction,
    Doctor,
    Escalation,
    IdentityKind,
    InboundLead,
    Message,
    User,
)
from shared.db.models.crm import TagStatus

RECENT_MESSAGES = 5
MAX_PROMISES = 6
MAX_NOTES = 3
SNIPPET = 220


def _snip(text: str | None, n: int = SNIPPET) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _name(user: User | None) -> str | None:
    return (user.full_name or user.username or user.email) if user else None


@dataclass
class Briefing:
    customer_id: str
    name: str
    private: bool = False
    phones: list[str] = field(default_factory=list)
    language: str | None = None
    stage: str | None = None
    tags: list[str] = field(default_factory=list)
    owner: dict | None = None
    lead: dict | None = None
    summary: str | None = None
    last_said: str | None = None
    messages: list[dict] = field(default_factory=list)
    escalation: dict | None = None
    promises: list[dict] = field(default_factory=list)
    owed_to_us_paise: int = 0
    owed_by_us_paise: int = 0
    next_visit: dict | None = None
    last_call: dict | None = None
    notes: list[dict] = field(default_factory=list)
    headline: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def headline(b: Briefing) -> str:
    """One line for a push notification: who, what they want, what is pending."""
    parts = [b.name]
    if b.lead and b.lead.get("query"):
        parts.append(f"asked: {_snip(b.lead['query'], 60)}")
    elif b.last_said:
        parts.append(f"said: {_snip(b.last_said, 60)}")
    if b.escalation:
        parts.append(f"escalated: {_snip(b.escalation['reason'], 40)}")
    if b.owed_to_us_paise:
        parts.append(f"owes ₹{b.owed_to_us_paise // 100:,}")
    overdue = sum(1 for p in b.promises if p["overdue"])
    if overdue:
        parts.append(f"{overdue} overdue promise{'s' if overdue != 1 else ''}")
    return " · ".join(parts)


async def build(db: AsyncSession, *, business_id: uuid.UUID, customer_id: uuid.UUID, private_ok: bool = True) -> Briefing | None:
    """The briefing for one customer, or None if they are not this business's."""
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.business_id != business_id:
        return None

    out = Briefing(customer_id=str(customer.id), name=customer.display_name or "Unnamed customer")
    if customer.is_private and not private_ok:
        out.private = True
        out.headline = f"{out.name} · private chat"
        return out

    now = datetime.now(timezone.utc)
    out.language = customer.preferred_language
    out.stage = customer.stage

    phones = await db.execute(
        select(CustomerIdentity.value).where(
            CustomerIdentity.customer_id == customer.id, CustomerIdentity.kind == IdentityKind.phone,
        )
    )
    out.phones = list(phones.scalars().all())

    tags = await db.execute(
        select(CustomerTag.label).where(
            CustomerTag.customer_id == customer.id, CustomerTag.status == TagStatus.confirmed,
        )
    )
    out.tags = sorted(set(tags.scalars().all()))[:8]

    if customer.assigned_to_user_id:
        owner = await db.get(User, customer.assigned_to_user_id)
        out.owner = {"user_id": str(customer.assigned_to_user_id), "name": _name(owner)}

    lead = (await db.execute(
        select(InboundLead).where(InboundLead.customer_id == customer.id)
        .order_by(InboundLead.received_at.desc()).limit(1)
    )).scalars().first()
    if lead is not None:
        out.lead = {
            "source": lead.source, "query": _snip(lead.query), "received_at": lead.received_at.isoformat(),
        }

    intelligence = await db.get(CustomerIntelligence, customer.id)
    if intelligence is not None and intelligence.summary:
        out.summary = _snip(intelligence.summary, 500)

    recent = list((await db.execute(
        select(Message).where(Message.customer_id == customer.id, Message.content.is_not(None))
        .order_by(Message.occurred_at.desc()).limit(RECENT_MESSAGES)
    )).scalars().all())[::-1]
    out.messages = [
        {
            "who": "customer" if m.direction == Direction.inbound else "us",
            "channel": m.channel.value if hasattr(m.channel, "value") else str(m.channel),
            "text": _snip(m.content),
            "at": m.occurred_at.isoformat(),
        }
        for m in recent
    ]
    last_in = next((m for m in reversed(recent) if m.direction == Direction.inbound), None)
    out.last_said = _snip(last_in.content) if last_in else None

    escalation = (await db.execute(
        select(Escalation).where(
            Escalation.customer_id == customer.id, Escalation.status.in_(("open", "in_progress")),
        ).order_by(Escalation.created_at.desc()).limit(1)
    )).scalars().first()
    if escalation is not None:
        out.escalation = {
            "reason": _snip(escalation.reason, 160), "status": escalation.status,
            "at": escalation.created_at.isoformat(),
        }

    promises = (await db.execute(
        select(Commitment, OUTSTANDING.label("outstanding")).where(
            Commitment.customer_id == customer.id, Commitment.status == CommitmentStatus.open,
        ).order_by(Commitment.due_at.asc().nullslast())
    )).all()
    for commitment, outstanding in promises:
        due = commitment.due_at
        owed = int(outstanding or 0)
        if commitment.direction == CommitmentDirection.they_owe:
            out.owed_to_us_paise += owed
        else:
            out.owed_by_us_paise += owed
        if len(out.promises) < MAX_PROMISES:
            out.promises.append({
                "id": str(commitment.id),
                "direction": commitment.direction.value,
                "kind": commitment.kind.value,
                "description": _snip(commitment.description, 140),
                "due_at": due.isoformat() if due else None,
                "amount_paise": commitment.amount_paise,
                "outstanding_paise": owed if commitment.amount_paise is not None else None,
                "overdue": bool(due and due < now),
            })

    visit = (await db.execute(
        select(Appointment, Doctor.name).join(Doctor, Doctor.id == Appointment.doctor_id).where(
            Appointment.customer_id == customer.id, Appointment.starts_at >= now,
            Appointment.status.in_((AppointmentStatus.confirmed, AppointmentStatus.requested)),
        ).order_by(Appointment.starts_at).limit(1)
    )).first()
    if visit is not None:
        out.next_visit = {"at": visit[0].starts_at.isoformat(), "with": visit[1]}

    call = (await db.execute(
        select(Call).where(Call.customer_id == customer.id, Call.ended_at.is_not(None))
        .order_by(Call.started_at.desc()).limit(1)
    )).scalars().first()
    if call is not None:
        by = await db.get(User, call.handled_by_user_id) if call.handled_by_user_id else None
        out.last_call = {
            "at": call.started_at.isoformat(),
            "outcome": call.outcome,
            "summary": _snip(call.wrapup_note or call.summary, 300) or None,
            "by": _name(by) or ("the AI" if call.handled_by_user_id is None else None),
        }

    notes = (await db.execute(
        select(CustomerNote).where(CustomerNote.customer_id == customer.id)
        .order_by(CustomerNote.created_at.desc()).limit(MAX_NOTES)
    )).scalars().all()
    out.notes = [{"body": _snip(n.body, 240), "at": n.created_at.isoformat()} for n in notes]

    out.headline = headline(out)
    return out
