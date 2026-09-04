"""
Assembling what the agent needs to know.

The layer that makes an answer sound like this business rather than a
chatbot. Four tiers, each changing at a different rate, and the split exists
because of cost: a customer's whole history cannot go into every prompt, and
quality drops as context grows even when the budget allows it.

  business DNA        rarely changes    who they are, what they must not say
  customer profile    slowly            this person, compressed by the cold path
  recent turns        constantly        verbatim, the last few messages
  open commitments    per event         what is outstanding with this person

The compression is the point. The overnight worker turns two hundred messages
into five lines; the agent reads the five lines. That is what makes a reply
possible inside a phone call's latency budget, and it is why the cold path
exists at all.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared import verticals
from shared.db.models import (
    Appointment,
    Business,
    BusinessDNA,
    Case,
    CaseStatus,
    Commitment,
    CommitmentStatus,
    Customer,
    CustomerIdentity,
    CustomerIntelligence,
    Direction,
    Doctor,
    KnowledgeItem,
    Message,
    Order,
    Property,
)
from shared.scheduling import availability as scheduling_availability

# Doctors listed per reply, when the business has the scheduling capability.
# Bounded deliberately - this sits on the same latency-sensitive path as the
# rest of AgentContext.build(), and each doctor costs its own slot scan.
MAX_DOCTORS_IN_CONTEXT = 5

# Orders listed per reply, when the business has order_sync. A WISMO
# question is almost always about the most recent order, occasionally the
# one before it - never a customer's entire purchase history.
MAX_ORDERS_IN_CONTEXT = 3

# Properties listed per reply, when the business has property_listings.
# Deliberately the *customer's own* viewing history, not the agency's whole
# inventory - a portfolio can run to hundreds of listings, and dumping all
# of it into every reply would neither fit the budget nor answer what a
# customer actually asked. Full inventory search is a real, separate
# feature this pass does not attempt - see VERTICAL_TEMPLATES.md.
MAX_PROPERTIES_IN_CONTEXT = 5

# How much verbatim history to include. Enough for the thread to make sense,
# not so much that a chatty customer costs a fortune on every reply.
RECENT_TURNS = 20


@dataclass(slots=True)
class AgentContext:
    business_name: str
    vertical: str
    dna_summary: str | None
    tone: str | None
    policies: str | None
    known_gaps: list[str]
    # From the vertical template directly, not BusinessDNA - unlike
    # known_gaps there is no per-business "learned" bucket for this yet,
    # so it is looked up fresh from shared/verticals each time rather than
    # seeded and persisted. Concrete, vertical-specific triggers a generic
    # model might not otherwise treat as urgent (a clinic's "severe pain,
    # bleeding, or an emergency", a law firm's "any complaint about how
    # the matter is being handled").
    escalate_immediately: list[str]
    offerings: dict
    pricing_notes: str | None
    opening_hours: dict
    # Rendered text, not structured data - real, computed slots for whichever
    # doctors this business has, only populated when the vertical declares
    # the scheduling capability. None (not empty string) means "this business
    # has no scheduling", distinct from "has it, nothing free" - the two must
    # read differently to the model, per the never-invent-availability rule.
    availability: str | None
    # Same convention as availability: rendered text, None when the
    # vertical has no case_tracking capability, distinct from "has it, no
    # open cases for this customer".
    cases: str | None
    # Same convention again: rendered text, None when the vertical has no
    # order_sync capability, distinct from "has it, no orders on record".
    orders: str | None
    # Same convention again: rendered text, None when the vertical has no
    # property_listings capability, distinct from "has it, no viewings for
    # this customer yet". This is the customer's own viewing history, not
    # the agency's inventory - see MAX_PROPERTIES_IN_CONTEXT.
    properties: str | None

    customer_name: str | None
    customer_summary: str | None
    customer_since: str | None
    identities: list[str]

    # What the owner has written down. Injected whole - see the knowledge
    # module for why this is not a retrieval step.
    knowledge: list[dict] = field(default_factory=list)
    recent: list[dict] = field(default_factory=list)
    open_commitments: list[dict] = field(default_factory=list)
    # Every message id the agent was shown, so a draft can cite its sources
    # the same way a commitment does.
    context_message_ids: list[uuid.UUID] = field(default_factory=list)

    def render(self) -> str:
        """Lay it out for the model, in the order a person would want it."""
        lines: list[str] = [f"You are answering on behalf of {self.business_name}."]

        if self.dna_summary:
            lines.append(self.dna_summary)
        if self.tone:
            lines.append(f"\nHow this business speaks:\n{self.tone}")
        if self.policies:
            lines.append(f"\nRules you must follow:\n{self.policies}")
        if self.known_gaps:
            lines.append(
                "\nYou must NOT answer these - hand them to a human instead:\n"
                + "\n".join(f"- {g}" for g in self.known_gaps)
            )
        if self.escalate_immediately:
            lines.append(
                "\nEscalate immediately, without trying to answer first, if the "
                "message matches any of these:\n"
                + "\n".join(f"- {e}" for e in self.escalate_immediately)
            )
        if self.pricing_notes:
            lines.append(f"\nPricing:\n{self.pricing_notes}")
        if self.offerings:
            lines.append(f"\nWhat they offer:\n{self.offerings}")
        if self.opening_hours:
            lines.append(f"\nOpening hours:\n{self.opening_hours}")
        if self.availability is not None:
            lines.append(
                "\nReal current availability (only source of truth for booking "
                f"- do not invent times outside this):\n{self.availability}"
                if self.availability
                else "\nReal current availability: nothing free in the near term. "
                "Escalate any booking request rather than guessing."
            )

        for item in self.knowledge:
            # A price list is authoritative and must be quoted exactly; an FAQ
            # is a suggested answer the agent may rephrase. Saying which is
            # which changes how it uses them.
            weight = (
                "quote this exactly"
                if item["kind"] in ("price_list", "policy", "hours")
                else "use as reference"
            )
            lines.append(f"\n{item['title']} ({weight}):\n{item['content']}")

        lines.append("\n---\n")
        who = self.customer_name or "This customer"
        lines.append(f"You are talking to: {who}")
        if self.customer_since:
            lines.append(f"First contact: {self.customer_since}")
        if self.customer_summary:
            lines.append(f"\nWhat you know about them:\n{self.customer_summary}")
        if self.cases is not None:
            lines.append(
                f"\nTheir case(s) with you - the only source of truth for status "
                f"questions, never a guess, and never mention any case that is not "
                f"listed here (it belongs to someone else):\n{self.cases}"
                if self.cases
                else "\nNo case on record for this customer yet. Escalate rather "
                "than guessing at a status."
            )
        if self.orders is not None:
            lines.append(
                f"\nTheir recent order(s) - the only source of truth for a "
                f"'where is my order' question, never a guess:\n{self.orders}"
                if self.orders
                else "\nNo order on record for this customer yet. If they say "
                "they placed one, escalate rather than guessing at a status."
            )
        if self.properties is not None:
            lines.append(
                f"\nProperties they have viewed or have a viewing booked for - "
                f"the only source of truth for these listings' price and status, "
                f"never a guess, and never state a price or status for any other "
                f"property from memory:\n{self.properties}"
                if self.properties
                else "\nNo viewing on record for this customer yet."
            )

        if self.open_commitments:
            lines.append("\nOutstanding between you:")
            for c in self.open_commitments:
                amount = (
                    f" ({c['amount']})" if c.get("amount") else ""
                )
                due = f", due {c['due']}" if c.get("due") else ""
                side = "You owe them" if c["direction"] == "we_owe" else "They owe you"
                lines.append(f"- {side}: {c['description']}{amount}{due}")

        lines.append("\n---\n\nThe conversation so far:")
        for turn in self.recent:
            speaker = "Customer" if turn["direction"] == "inbound" else "You"
            lines.append(f"[{turn['channel']}] {speaker}: {turn['text']}")

        return "\n".join(lines)


async def build(
    business_id: uuid.UUID, customer_id: uuid.UUID, db: AsyncSession
) -> AgentContext:
    """
    Gather everything the agent should know before it writes a word.

    Four small indexed reads, deliberately - this sits on the path a caller
    waits through, so it must stay in the tens of milliseconds.
    """
    business = await db.get(Business, business_id)
    dna = await db.get(BusinessDNA, business_id)
    customer = await db.get(Customer, customer_id)
    intelligence = await db.get(CustomerIntelligence, customer_id)

    identities = (
        await db.execute(
            select(CustomerIdentity.value).where(
                CustomerIdentity.customer_id == customer_id
            )
        )
    ).scalars().all()

    rows = await db.execute(
        select(Message)
        .where(Message.customer_id == customer_id)
        .order_by(Message.occurred_at.desc())
        .limit(RECENT_TURNS)
    )
    messages = list(rows.scalars().all())[::-1]

    knowledge = await db.execute(
        select(KnowledgeItem)
        .where(
            KnowledgeItem.business_id == business_id,
            KnowledgeItem.active == True,  # noqa: E712
        )
        .order_by(KnowledgeItem.kind)
    )
    knowledge_items = list(knowledge.scalars().all())

    commitments = await db.execute(
        select(Commitment)
        .where(
            Commitment.customer_id == customer_id,
            Commitment.status == CommitmentStatus.open,
        )
        .order_by(Commitment.due_at.asc().nullslast())
    )

    gaps = []
    if dna and isinstance(dna.known_gaps, dict):
        gaps = list(dna.known_gaps.get("from_template", [])) + list(
            dna.known_gaps.get("learned", [])
        )

    escalate_immediately = (
        verticals.get(business.vertical).get("escalate_immediately", []) if business else []
    )

    availability_text: str | None = None
    if business and verticals.has_capability(business.vertical, "scheduling"):
        doctors = (
            await db.execute(
                select(Doctor)
                .where(Doctor.business_id == business_id, Doctor.active == True)  # noqa: E712
                .limit(MAX_DOCTORS_IN_CONTEXT)
            )
        ).scalars().all()
        doctor_lines = []
        for doctor in doctors:
            slots = await scheduling_availability.next_open_slots(
                db, business=business, doctor=doctor, count=3
            )
            # ISO, not a human-friendly format: this is what the model must
            # echo back verbatim in book_slot to actually reserve one, and an
            # LLM can write a natural-sounding reply from an ISO timestamp
            # just fine - the customer never sees this string directly.
            times = ", ".join(s.starts_at.isoformat() for s in slots)
            doctor_lines.append(f"- {doctor.name}: {times or 'nothing free in the next two weeks'}")
        availability_text = "\n".join(doctor_lines)

    cases_text: str | None = None
    if business and verticals.has_capability(business.vertical, "case_tracking"):
        rows = (
            await db.execute(
                select(Case)
                .where(Case.customer_id == customer_id, Case.status != CaseStatus.closed)
                .order_by(Case.next_hearing_at.asc().nullslast())
            )
        ).scalars().all()
        case_lines = []
        for c in rows:
            hearing = f", next hearing {c.next_hearing_at.isoformat()}" if c.next_hearing_at else ""
            number = f" ({c.case_number})" if c.case_number else ""
            case_lines.append(f"- {c.title}{number} - status: {c.status.value}{hearing}")
        cases_text = "\n".join(case_lines)

    orders_text: str | None = None
    if business and verticals.has_capability(business.vertical, "order_sync"):
        rows = (
            await db.execute(
                select(Order)
                .where(Order.customer_id == customer_id)
                .order_by(Order.placed_at.desc())
                .limit(MAX_ORDERS_IN_CONTEXT)
            )
        ).scalars().all()
        order_lines = []
        for o in rows:
            number = f" #{o.order_number}" if o.order_number else ""
            tracking = f", tracking {o.tracking_number} ({o.carrier})" if o.tracking_number else ""
            status_value = o.status.value if hasattr(o.status, "value") else str(o.status)
            order_lines.append(
                f"- Order{number}, placed {o.placed_at.strftime('%d %b')} - "
                f"status: {status_value}{tracking}"
            )
        orders_text = "\n".join(order_lines)

    properties_text: str | None = None
    if business and verticals.has_capability(business.vertical, "property_listings"):
        rows = (
            await db.execute(
                select(Appointment, Property)
                .join(Property, Property.id == Appointment.property_id)
                .where(Appointment.customer_id == customer_id)
                .order_by(Appointment.starts_at.desc())
            )
        ).all()
        seen: set[uuid.UUID] = set()
        property_lines = []
        for appt, prop in rows:
            if prop.id in seen or len(property_lines) >= MAX_PROPERTIES_IN_CONTEXT:
                continue
            seen.add(prop.id)
            price = f"₹{prop.price_paise / 100:,.0f}" if prop.price_paise else "price on request"
            if prop.price_period == "monthly":
                price += "/month"
            when = appt.starts_at.strftime("%d %b")
            status_value = prop.status.value if hasattr(prop.status, "value") else str(prop.status)
            property_lines.append(
                f"- {prop.title} ({prop.locality or 'location on file'}), {price} - "
                f"status: {status_value}, viewing {when}"
            )
        properties_text = "\n".join(property_lines)

    return AgentContext(
        business_name=business.name if business else "this business",
        vertical=business.vertical if business else "general",
        dna_summary=dna.summary if dna else None,
        tone=dna.tone if dna else None,
        policies=dna.policies if dna else None,
        known_gaps=gaps,
        escalate_immediately=escalate_immediately,
        offerings=(dna.offerings if dna else {}) or {},
        pricing_notes=dna.pricing_notes if dna else None,
        opening_hours=(dna.opening_hours if dna else {}) or {},
        availability=availability_text,
        cases=cases_text,
        orders=orders_text,
        properties=properties_text,
        knowledge=[
            {
                "title": k.title,
                "kind": k.kind.value if hasattr(k.kind, "value") else str(k.kind),
                "content": k.content,
            }
            for k in knowledge_items
        ],
        customer_name=customer.display_name if customer else None,
        customer_summary=intelligence.summary if intelligence else None,
        customer_since=(
            customer.first_seen_at.strftime("%d %B %Y")
            if customer and customer.first_seen_at
            else None
        ),
        identities=list(identities),
        recent=[
            {
                "channel": m.channel.value if hasattr(m.channel, "value") else m.channel,
                "direction": (
                    m.direction.value if hasattr(m.direction, "value") else m.direction
                ),
                "text": (m.content or "").strip()[:600],
            }
            for m in messages
            if (m.content or "").strip()
        ],
        open_commitments=[
            {
                "direction": (
                    c.direction.value if hasattr(c.direction, "value") else c.direction
                ),
                "description": c.description,
                "amount": f"₹{c.amount_paise / 100:,.0f}" if c.amount_paise else None,
                "due": c.due_at.strftime("%d %b") if c.due_at else None,
            }
            for c in commitments.scalars().all()
        ],
        context_message_ids=[m.id for m in messages],
    )


def now_line() -> str:
    """Today's date, so the agent can reason about 'Friday' and 'next week'."""
    return datetime.now(timezone.utc).strftime("%A, %d %B %Y")
