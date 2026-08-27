"""
Analytics.

Every competitor reports messages sent, delivered and read. Those are numbers
about the tool, not about the business — an owner who learns they sent 412
messages last month has learned nothing they can act on.

This answers the questions a business actually has:

    what am I owed, and how old is it
    did I keep the promises I made
    where is money getting stuck
    what does my agent keep failing to answer

The receivables ageing view is deliberately in the shape a CA already reads —
0-30, 31-60, 61-90, 90+ — because the person who can act on it is often the
accountant, and giving them something in an unfamiliar format means they
ignore it. It is also, three weeks before it reaches the books, the only place
this information exists.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import case, func, select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import (
    BusinessMember,
    Commitment,
    CommitmentDirection,
    CommitmentStatus,
    Customer,
    Direction,
    DraftAction,
    DraftStatus,
    Message,
    MessageDraft,
    User,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])


class AgeingBucket(BaseModel):
    label: str
    count: int
    amount_paise: int
    amount: str


class Receivables(BaseModel):
    """What a CA would recognise, weeks before it reaches the books."""

    total_paise: int
    total: str
    buckets: list[AgeingBucket]
    oldest_days: int | None
    worst_customer: dict | None


class Kept(BaseModel):
    """Did the business do what it said it would."""

    promised: int
    met: int
    missed: int
    still_open: int
    kept_rate: float | None
    note: str


class ChannelActivity(BaseModel):
    channel: str
    inbound: int
    outbound: int
    customers: int


class AgentPerformance(BaseModel):
    drafted: int
    approved: int
    edited: int
    rejected: int
    escalated: int
    approval_rate: float | None
    edit_rate: float | None
    note: str
    top_gaps: list[dict]


class TeamMemberPerformance(BaseModel):
    user_id: str
    full_name: str | None
    email: str
    messages_sent: int
    # How many of those sends were a genuine reply to a preceding inbound
    # message - a bulk-sent template with nobody waiting on it doesn't count.
    replies_counted: int
    avg_first_response_minutes: float | None
    commitments_resolved: int
    avg_resolution_hours: float | None


class TeamPerformance(BaseModel):
    days: int
    members: list[TeamMemberPerformance]


def _money(paise: int) -> str:
    return f"₹{paise / 100:,.0f}"


@router.get("/receivables", response_model=Receivables)
async def receivables(current_user: CurrentUserDep, db: DbDep) -> Receivables:
    """
    What you are owed, by how late it is.

    The view a CA reads without explanation, built from conversations rather
    than invoices — which is why it exists weeks before the books do.
    """
    business_id = current_user.business
    now = datetime.now(timezone.utc)

    rows = (
        await db.execute(
            select(Commitment)
            .where(
                Commitment.business_id == business_id,
                Commitment.direction == CommitmentDirection.they_owe,
                Commitment.status == CommitmentStatus.open,
                Commitment.amount_paise.isnot(None),
            )
            .order_by(Commitment.due_at.asc().nullslast())
        )
    ).scalars().all()

    buckets = {
        "Not yet due": [0, 0],
        "0-30 days": [0, 0],
        "31-60 days": [0, 0],
        "61-90 days": [0, 0],
        "Over 90 days": [0, 0],
    }
    oldest_days: int | None = None
    by_customer: dict = {}

    for c in rows:
        amount = c.amount_paise or 0
        due = c.due_at
        if due and due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)

        if due is None or due > now:
            key = "Not yet due"
            days = 0
        else:
            days = (now - due).days
            oldest_days = max(oldest_days or 0, days)
            if days <= 30:
                key = "0-30 days"
            elif days <= 60:
                key = "31-60 days"
            elif days <= 90:
                key = "61-90 days"
            else:
                key = "Over 90 days"

        buckets[key][0] += 1
        buckets[key][1] += amount

        entry = by_customer.setdefault(
            c.customer_id, {"amount": 0, "count": 0, "oldest": 0}
        )
        entry["amount"] += amount
        entry["count"] += 1
        entry["oldest"] = max(entry["oldest"], days)

    worst = None
    if by_customer:
        customer_id, data = max(by_customer.items(), key=lambda kv: kv[1]["amount"])
        customer = await db.get(Customer, customer_id)
        worst = {
            "customer_id": str(customer_id),
            "name": customer.display_name if customer else None,
            "amount": _money(data["amount"]),
            "promises": data["count"],
            "oldest_days": data["oldest"],
        }

    total = sum(v[1] for v in buckets.values())
    return Receivables(
        total_paise=total,
        total=_money(total),
        buckets=[
            AgeingBucket(label=k, count=v[0], amount_paise=v[1], amount=_money(v[1]))
            for k, v in buckets.items()
        ],
        oldest_days=oldest_days,
        worst_customer=worst,
    )


@router.get("/kept", response_model=Kept)
async def kept_promises(
    current_user: CurrentUserDep, db: DbDep, days: int = Query(default=90, le=365)
) -> Kept:
    """
    Whether the business keeps its own word.

    The number nobody else can produce, and the one an owner is quietly
    nervous about. Reputation is built here, not in receivables.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (
        await db.execute(
            select(Commitment.status, func.count(Commitment.id))
            .where(
                Commitment.business_id == current_user.business,
                Commitment.direction == CommitmentDirection.we_owe,
                Commitment.created_at >= since,
            )
            .group_by(Commitment.status)
        )
    ).all()

    counts = {str(s.value if hasattr(s, "value") else s): n for s, n in rows}
    met = counts.get("met", 0)
    missed = counts.get("missed", 0)
    still_open = counts.get("open", 0) + counts.get("unconfirmed", 0)
    resolved = met + missed
    promised = resolved + still_open

    rate = round(met / resolved, 3) if resolved else None

    if promised == 0:
        note = "No promises recorded yet in this period."
    elif rate is None:
        note = f"{still_open} promise(s) outstanding, none resolved yet."
    elif rate >= 0.9:
        note = f"You kept {met} of {resolved} promises. That is a good record."
    elif rate >= 0.7:
        note = (
            f"You kept {met} of {resolved} promises. {missed} were missed — "
            "worth looking at which kind."
        )
    else:
        note = (
            f"Only {met} of {resolved} promises were kept. Customers notice "
            "this before they say anything about it."
        )

    return Kept(
        promised=promised,
        met=met,
        missed=missed,
        still_open=still_open,
        kept_rate=rate,
        note=note,
    )


@router.get("/channels", response_model=list[ChannelActivity])
async def channel_activity(
    current_user: CurrentUserDep, db: DbDep, days: int = Query(default=30, le=365)
) -> list[ChannelActivity]:
    """
    Where business actually happens.

    Useful for deciding what to connect next, and for noticing when a channel
    a business assumed was busy turns out not to be.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (
        await db.execute(
            select(
                Message.channel,
                func.sum(case((Message.direction == Direction.inbound, 1), else_=0)),
                func.sum(case((Message.direction == Direction.outbound, 1), else_=0)),
                func.count(func.distinct(Message.customer_id)),
            )
            .where(
                Message.business_id == current_user.business,
                Message.occurred_at >= since,
            )
            .group_by(Message.channel)
        )
    ).all()

    return sorted(
        (
            ChannelActivity(
                channel=str(ch.value if hasattr(ch, "value") else ch),
                inbound=int(inbound or 0),
                outbound=int(outbound or 0),
                customers=int(customers or 0),
            )
            for ch, inbound, outbound, customers in rows
        ),
        key=lambda c: c.inbound + c.outbound,
        reverse=True,
    )


@router.get("/agent", response_model=AgentPerformance)
async def agent_performance(
    current_user: CurrentUserDep, db: DbDep, days: int = Query(default=30, le=365)
) -> AgentPerformance:
    """
    How the agent is doing, measured honestly.

    Approval rate says whether it is trusted. Edit rate says whether it is
    nearly right or fundamentally wrong — a business that approves everything
    after rewriting it has an agent that is not working, and a raw approval
    rate would hide that.
    """
    business_id = current_user.business
    since = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (
        await db.execute(
            select(MessageDraft).where(
                MessageDraft.business_id == business_id,
                MessageDraft.created_at >= since,
            )
        )
    ).scalars().all()

    drafted = len(rows)
    approved = sum(1 for d in rows if d.status == DraftStatus.sent)
    edited = sum(1 for d in rows if d.status == DraftStatus.sent and d.edited_body)
    rejected = sum(1 for d in rows if d.status == DraftStatus.rejected)
    escalated = sum(1 for d in rows if d.action == DraftAction.escalate)

    reviewed = approved + rejected
    approval_rate = round(approved / reviewed, 3) if reviewed else None
    edit_rate = round(edited / approved, 3) if approved else None

    if drafted == 0:
        note = "The agent has not drafted anything yet."
    elif approval_rate is None:
        note = f"{drafted} draft(s) waiting for review."
    elif approval_rate >= 0.9 and (edit_rate or 0) < 0.3:
        note = (
            "The agent is writing replies you send largely unchanged. Worth "
            "considering whether it should send some of them itself."
        )
    elif (edit_rate or 0) >= 0.5:
        note = (
            "You approve most drafts but rewrite half of them. That usually "
            "means something is missing from your business details rather than "
            "the agent being wrong."
        )
    else:
        note = f"{approved} approved, {rejected} rejected out of {reviewed} reviewed."

    gap_counts: dict[str, int] = {}
    for d in rows:
        if d.action == DraftAction.escalate and d.gap:
            key = d.gap.strip()[:70]
            gap_counts[key] = gap_counts.get(key, 0) + 1

    return AgentPerformance(
        drafted=drafted,
        approved=approved,
        edited=edited,
        rejected=rejected,
        escalated=escalated,
        approval_rate=approval_rate,
        edit_rate=edit_rate,
        note=note,
        top_gaps=[
            {"gap": g, "times": n}
            for g, n in sorted(gap_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
        ],
    )


@router.get("/team", response_model=TeamPerformance)
async def team_performance(
    current_user: CurrentUserDep, db: DbDep, days: int = Query(default=30, le=365)
) -> TeamPerformance:
    """
    How the humans on this business's team are actually doing - not the AI.

    First-response time only counts a send that a specific team member sent
    (Message.sent_by_user_id) and that was a genuine reply to a customer who
    was waiting - a campaign blast or an unreviewed AI send never attributes
    to anyone here, on purpose. Resolution time is scoped to Commitments only:
    Case has no closed_at, and guessing one from updated_at would present a
    number this product's whole premise is built to never present - a fact
    with nothing behind it.
    """
    business_id = current_user.business
    since = datetime.now(timezone.utc) - timedelta(days=days)

    members = (
        await db.execute(
            select(BusinessMember.user_id, User.full_name, User.email)
            .join(User, User.id == BusinessMember.user_id)
            .where(BusinessMember.business_id == business_id, User.is_active == True)  # noqa: E712
        )
    ).all()

    rows = (
        await db.execute(
            select(
                Message.customer_id, Message.direction, Message.occurred_at,
                Message.sent_by_user_id,
            )
            .where(Message.business_id == business_id, Message.occurred_at >= since)
            .order_by(Message.customer_id, Message.occurred_at)
        )
    ).all()

    sends_by_user: dict = {}
    response_seconds_by_user: dict = {}
    pending_inbound_at = None
    current_customer = None

    for customer_id, direction, occurred_at, sent_by in rows:
        if customer_id != current_customer:
            current_customer = customer_id
            pending_inbound_at = None

        if direction == Direction.inbound:
            if pending_inbound_at is None:
                pending_inbound_at = occurred_at
            continue

        # Outbound.
        if sent_by is not None:
            sends_by_user[sent_by] = sends_by_user.get(sent_by, 0) + 1
            if pending_inbound_at is not None:
                delta = (occurred_at - pending_inbound_at).total_seconds()
                response_seconds_by_user.setdefault(sent_by, []).append(delta)
        # Any outbound reply ends the customer's wait, whoever or whatever sent it.
        pending_inbound_at = None

    commitment_rows = (
        await db.execute(
            select(
                Commitment.confirmed_by_user_id, Commitment.created_at, Commitment.resolved_at,
            )
            .where(
                Commitment.business_id == business_id,
                Commitment.confirmed_by_user_id.isnot(None),
                Commitment.resolved_at.isnot(None),
                Commitment.resolved_at >= since,
            )
        )
    ).all()

    resolution_hours_by_user: dict = {}
    for user_id, created_at, resolved_at in commitment_rows:
        hours = (resolved_at - created_at).total_seconds() / 3600
        resolution_hours_by_user.setdefault(user_id, []).append(hours)

    out = []
    for user_id, full_name, email in members:
        response_times = response_seconds_by_user.get(user_id, [])
        resolution_times = resolution_hours_by_user.get(user_id, [])
        out.append(
            TeamMemberPerformance(
                user_id=str(user_id),
                full_name=full_name,
                email=email,
                messages_sent=sends_by_user.get(user_id, 0),
                replies_counted=len(response_times),
                avg_first_response_minutes=(
                    round(sum(response_times) / len(response_times) / 60, 1)
                    if response_times else None
                ),
                commitments_resolved=len(resolution_times),
                avg_resolution_hours=(
                    round(sum(resolution_times) / len(resolution_times), 1)
                    if resolution_times else None
                ),
            )
        )

    out.sort(key=lambda m: m.messages_sent, reverse=True)
    return TeamPerformance(days=days, members=out)


@router.get("/overview")
async def overview(current_user: CurrentUserDep, db: DbDep) -> dict:
    """
    The one call a dashboard makes.

    Deliberately money first. Message counts are at the bottom because they
    are the least useful thing on the page, not the most.
    """
    # Explicit values, not the endpoints' defaults: calling a FastAPI handler
    # directly skips dependency resolution, so a Query(default=30) arrives as
    # a Query object rather than an int.
    money = await receivables(current_user, db)
    kept = await kept_promises(current_user, db, days=90)
    channels = await channel_activity(current_user, db, days=30)
    agent = await agent_performance(current_user, db, days=30)

    return {
        "owed_to_you": money.total,
        "owed_to_you_paise": money.total_paise,
        "oldest_debt_days": money.oldest_days,
        "biggest_debtor": money.worst_customer,
        "promises_kept": kept.kept_rate,
        "promises_note": kept.note,
        "channels": [c.model_dump() for c in channels],
        "agent": {
            "drafted": agent.drafted,
            "approval_rate": agent.approval_rate,
            "note": agent.note,
            "top_gaps": agent.top_gaps,
        },
    }
