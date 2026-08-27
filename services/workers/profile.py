"""
The nightly pass that keeps customer profiles current.

Runs on its own queue, after the day's messages have landed. Its output is
what the live agent reads instead of raw history - so this is the job that
decides whether a reply arrives in 800ms or four seconds.

Only recompresses customers whose conversation actually moved. A business
with 2,000 customers and 30 active conversations should pay for 30 summaries,
not 2,000.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.ai import compression
from shared.ai.client import AIError
from shared.billing import usage
from shared.crm import tagging
from shared.db import queue
from shared.db.models import (
    Business,
    BusinessDNA,
    Commitment,
    CommitmentStatus,
    Customer,
    CustomerIntelligence,
    Job,
    Message,
    UsageEventType,
)
from shared.db.worker_runner import run_worker_process
from shared.utils.logging import get_logger

logger = get_logger(__name__)

QUEUE = "compress_customer"

# A profile older than this is stale even if nothing new arrived - "she owes
# you Rs 4,500" ages badly.
MAX_PROFILE_AGE = timedelta(days=30)


async def _business_context(business_id: uuid.UUID, db: AsyncSession) -> str:
    business = await db.get(Business, business_id)
    dna = await db.get(BusinessDNA, business_id)
    parts = [f"{business.name} ({business.vertical})" if business else "A business"]
    if dna and dna.summary:
        parts.append(dna.summary)
    return "\n".join(parts)


async def compress_customer(customer_id: uuid.UUID, db: AsyncSession) -> bool:
    """
    Rewrite one customer's profile. Returns True if it changed.
    """
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.is_private:
        # A private customer is never read by the agent, so there is nothing
        # worth compressing and nothing that should be.
        return False

    rows = await db.execute(
        select(Message)
        .where(Message.customer_id == customer_id)
        .order_by(Message.occurred_at)
        .limit(compression.MAX_MESSAGES)
    )
    messages = [
        {
            "id": m.id,
            "direction": m.direction.value if hasattr(m.direction, "value") else m.direction,
            "channel": m.channel.value if hasattr(m.channel, "value") else m.channel,
            "text": m.content,
            "occurred_at": m.occurred_at,
        }
        for m in rows.scalars().all()
    ]

    commitment_rows = await db.execute(
        select(Commitment)
        .where(Commitment.customer_id == customer_id)
        .order_by(Commitment.due_at.asc().nullslast())
    )
    commitments = [
        {
            "direction": c.direction.value if hasattr(c.direction, "value") else c.direction,
            "description": c.description,
            "amount": f"₹{c.amount_paise / 100:,.0f}" if c.amount_paise else None,
            "due": c.due_at.strftime("%d %b %Y") if c.due_at else None,
            "status": c.status.value if hasattr(c.status, "value") else c.status,
        }
        for c in commitment_rows.scalars().all()
    ]

    try:
        profile = await compression.compress(
            messages=messages,
            commitments=commitments,
            customer_name=customer.display_name,
            first_seen=customer.first_seen_at,
            business_context=await _business_context(customer.business_id, db),
        )
    except AIError:
        raise  # let the queue retry with backoff

    if profile is None:
        return False

    usage.record(
        business_id=customer.business_id,
        event_type=UsageEventType.ai_profile_compression,
        # Not tied to one channel - a profile compresses a customer's whole
        # cross-channel history at once.
        channel="cross_channel",
        quantity=1,
        unit="call",
        krova_cost_paise=profile.cost_paise,
        source_type="customer",
        source_id=customer.id,
        db=db,
    )

    open_count = sum(1 for c in commitments if c["status"] == "open")
    outstanding = sum(
        c.amount_paise or 0
        for c in (await db.execute(
            select(Commitment).where(
                Commitment.customer_id == customer_id,
                Commitment.status == CommitmentStatus.open,
                Commitment.direction == "they_owe",
            )
        )).scalars().all()
    )

    # Which channel they actually reply on, learned rather than declared.
    inbound_channels: dict[str, int] = {}
    for m in messages:
        if m["direction"] == "inbound":
            inbound_channels[m["channel"]] = inbound_channels.get(m["channel"], 0) + 1
    preferred = max(inbound_channels, key=inbound_channels.get) if inbound_channels else None

    existing = await db.get(CustomerIntelligence, customer_id)
    if existing is None:
        existing = CustomerIntelligence(
            customer_id=customer_id, business_id=customer.business_id
        )
        db.add(existing)

    existing.summary = profile.summary
    existing.preferences = profile.preferences
    existing.health_score = profile.health_score
    existing.open_commitments = open_count
    existing.outstanding_paise = int(outstanding)
    existing.preferred_channel = preferred
    existing.source_message_ids = profile.source_message_ids
    existing.computed_at = datetime.now(timezone.utc)

    suggested = await tagging.apply_suggestions(
        customer_id, customer.business_id, existing, db
    )
    if suggested:
        logger.info(
            "suggested %s tag(s) for customer=%s from the refreshed profile",
            suggested, customer_id,
        )

    logger.info(
        "profile compressed customer=%s score=%s from %s messages cost=%sp",
        customer_id,
        profile.health_score,
        len(profile.source_message_ids),
        profile.cost_paise,
    )
    return True


async def queue_stale(business_id: uuid.UUID, db: AsyncSession, *, limit: int = 200) -> int:
    """
    Queue customers whose profile no longer reflects their conversation.

    Only what moved. A business with 2,000 customers and 30 active
    conversations should pay for 30 summaries.
    """
    now = datetime.now(timezone.utc)

    rows = await db.execute(
        select(Customer, CustomerIntelligence)
        .outerjoin(
            CustomerIntelligence, CustomerIntelligence.customer_id == Customer.id
        )
        .where(
            Customer.business_id == business_id,
            Customer.is_private == False,  # noqa: E712
        )
        .order_by(Customer.last_contact_at.desc().nullslast())
        .limit(limit * 4)
    )

    queued = 0
    for customer, intelligence in rows.all():
        if queued >= limit:
            break

        if intelligence is None or intelligence.computed_at is None:
            stale = True
        else:
            computed = intelligence.computed_at
            if computed.tzinfo is None:
                computed = computed.replace(tzinfo=timezone.utc)
            last = customer.last_contact_at
            if last and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            # Recompress when the conversation moved since the last pass, or
            # when the profile is simply old - amounts and dates age badly.
            stale = (last is not None and last > computed) or (
                now - computed > MAX_PROFILE_AGE
            )

        if stale:
            await queue.enqueue(QUEUE, {"customer_id": str(customer.id)}, db)
            queued += 1

    if queued:
        logger.info("queued %s customer profile(s) for compression", queued)
    return queued


async def _run_job(job: Job, db: AsyncSession) -> None:
    raw_id = (job.payload or {}).get("customer_id")
    if not raw_id:
        await queue.fail(job, "job payload has no customer_id", db)
        return
    try:
        await compress_customer(uuid.UUID(raw_id), db)
        await queue.complete(job, db)
    except Exception as exc:  # noqa: BLE001 - the queue decides what to do next
        await queue.fail(job, f"{type(exc).__name__}: {exc}", db)


if __name__ == "__main__":
    run_worker_process(QUEUE, _run_job, worker_name="compress_customer")
