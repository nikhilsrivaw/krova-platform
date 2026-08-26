"""
The cold path.

Reads conversations, works out what was promised, and writes it down with the
evidence attached. Nobody is waiting on this, which is exactly what makes it
useful: it can afford to think, and the live agent then reads the conclusions
in a few milliseconds.

Runs as its own process. Claims jobs from Postgres, so several can run at once
without coordinating - SKIP LOCKED means two workers never take the same job.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared import verticals
from shared.ai import commitments as extractor
from shared.ai import signals as signal_extractor
from shared.ai.client import AIError
from shared.billing import usage
from shared.db import queue
from shared.db.models import (
    Business,
    BusinessDNA,
    Commitment,
    CommitmentStatus,
    Customer,
    Direction,
    Doctor,
    Insight,
    Job,
    Message,
    UsageEventType,
)
from shared.db.session import AsyncSessionLocal
from shared.db.worker_runner import run_worker
from shared.scheduling import notify
from shared.scheduling.from_commitment import try_book_from_commitment
from shared.utils.logging import get_logger

logger = get_logger(__name__)

QUEUE = "analyse_message"
BUSINESS_QUEUE = "analyse_business"

# How much of a conversation to read. Enough for a promise and its context;
# not so much that a chatty customer costs a fortune every time they write.
CONTEXT_MESSAGES = 40

# How far back a nightly business sweep looks for messages the real-time
# trigger never got to - old enough to catch a message ingested with
# enqueue_analysis=False (a backfill) or one whose real-time job exhausted
# its retries, not so old that a sweep re-reads a customer's entire history
# every single night.
BACKFILL_WINDOW = timedelta(days=2)


async def _business_context(business_id: uuid.UUID, db: AsyncSession) -> str:
    """
    What the model needs to know about this business.

    Comes from the vertical template at signup and improves as the business
    fills it in, so a clinic's extraction is done knowing it is a clinic.
    """
    business = await db.get(Business, business_id)
    dna = await db.get(BusinessDNA, business_id)

    parts = [f"{business.name} ({business.vertical})" if business else "A business"]
    if dna:
        if dna.summary:
            parts.append(dna.summary)
        if dna.pricing_notes:
            parts.append(f"Pricing: {dna.pricing_notes}")
    return "\n".join(parts)


async def _conversation(
    business_id: uuid.UUID, customer_id: uuid.UUID, db: AsyncSession
) -> list[dict]:
    """The recent history with one customer, oldest first, across every channel."""
    result = await db.execute(
        select(Message)
        .where(Message.business_id == business_id, Message.customer_id == customer_id)
        .order_by(Message.occurred_at.desc())
        .limit(CONTEXT_MESSAGES)
    )
    rows = list(result.scalars().all())[::-1]
    return [
        {
            "id": m.id,
            "direction": m.direction.value if hasattr(m.direction, "value") else m.direction,
            "text": m.content,
            "occurred_at": m.occurred_at,
            # Content we read out of a file the customer sent, rather than
            # words they typed. The distinction changes how it is read.
            "is_attachment": bool((m.media or {}).get("read_as")),
        }
        for m in rows
    ]


async def _existing_quotes(customer_id: uuid.UUID, db: AsyncSession) -> set[str]:
    """
    What we have already recorded for this customer.

    Every new message re-reads the conversation, so without this the same
    promise would be recorded again on every reply - and an owner would watch
    one invoice multiply into five.
    """
    result = await db.execute(
        select(Commitment.source_quote).where(Commitment.customer_id == customer_id)
    )
    return {(q or "").strip().lower() for q in result.scalars().all()}


async def _existing_signal_titles(customer_id: uuid.UUID, db: AsyncSession) -> set[str]:
    """
    Same problem _existing_quotes solves, for signals: without this, a
    re-read conversation would record the same bug report again on every
    reply. Insight has no source_quote field (it predates this capability),
    so title is the next best fingerprint - approximate, same as the quote
    match above, and good enough for the same reason.
    """
    result = await db.execute(select(Insight.title).where(Insight.customer_id == customer_id))
    return {(t or "").strip().lower() for t in result.scalars().all()}


async def _extract_signals(
    message: Message, conversation: list[dict], business_context: str, db: AsyncSession
) -> int:
    """
    Product feedback signals - bugs, feature requests, complaints, churn
    risk, praise - stored as Insight rows. Only called for a business whose
    vertical declares the product_feedback capability, gated by the caller.
    """
    try:
        extraction = await signal_extractor.extract(
            messages=conversation, business_context=business_context
        )
    except AIError:
        raise

    channel = message.channel.value if hasattr(message.channel, "value") else message.channel
    usage.record(
        business_id=message.business_id,
        event_type=UsageEventType.ai_signal_extraction,
        channel=channel,
        quantity=1,
        unit="call",
        krova_cost_paise=extraction.cost_paise,
        source_type="message",
        source_id=message.id,
        db=db,
    )

    already = await _existing_signal_titles(message.customer_id, db)
    stored = 0
    for found in extraction.signals:
        if found.title.strip().lower() in already:
            continue
        db.add(
            Insight(
                business_id=message.business_id,
                customer_id=message.customer_id,
                kind=found.kind,
                title=found.title,
                body=found.body,
                severity=found.severity,
                source_message_ids=found.source_message_ids,
                created_at=datetime.now(timezone.utc),
            )
        )
        already.add(found.title.strip().lower())
        stored += 1

    if stored:
        logger.info("stored %s product feedback signal(s) for message=%s", stored, message.id)
    return stored


async def analyse_message(message_id: uuid.UUID, db: AsyncSession) -> int:
    """
    Read the conversation this message belongs to and record any promises.

    Returns how many new commitments were stored.
    """
    message = await db.get(Message, message_id)
    if message is None:
        logger.warning("message %s no longer exists, skipping", message_id)
        return 0

    customer = await db.get(Customer, message.customer_id)
    if customer is None or customer.is_private:
        # A customer the owner marked private is never analysed. The message
        # stays stored; nothing is derived from it.
        return 0

    business = await db.get(Business, message.business_id)

    conversation = await _conversation(message.business_id, message.customer_id, db)
    if not conversation:
        return 0

    context = await _business_context(message.business_id, db)

    try:
        extraction = await extractor.extract(
            messages=conversation, business_context=context
        )
    except AIError:
        # Let the job retry with backoff rather than dropping the message.
        raise

    channel = message.channel.value if hasattr(message.channel, "value") else message.channel
    usage.record(
        business_id=message.business_id,
        event_type=UsageEventType.ai_commitment_extraction,
        channel=channel,
        quantity=1,
        unit="call",
        krova_cost_paise=extraction.cost_paise,
        source_type="message",
        source_id=message.id,
        db=db,
    )

    if business and verticals.has_capability(business.vertical, "product_feedback"):
        await _extract_signals(message, conversation, context, db)

    already = await _existing_quotes(message.customer_id, db)
    stored = 0

    for found in extraction.commitments:
        if found.source_quote.strip().lower() in already:
            continue

        confident = found.confidence >= extractor.CONFIRM_THRESHOLD
        commitment = Commitment(
            business_id=message.business_id,
            customer_id=message.customer_id,
            direction=found.direction,
            kind=found.kind,
            description=found.description,
            amount_paise=found.amount_paise,
            due_at=found.due_at,
            due_at_explicit=found.due_at_explicit,
            # Anything the model was unsure about waits for a human rather
            # than appearing in the ledger as fact.
            status=CommitmentStatus.open if confident else CommitmentStatus.unconfirmed,
            confidence=found.confidence,
            source_message_ids=found.source_message_ids,
            source_quote=found.source_quote,
        )
        db.add(commitment)
        already.add(found.source_quote.strip().lower())
        stored += 1

        # A confident "meeting" commitment for a scheduling-capable business
        # (a real appointment agreed on a call, or a WhatsApp thread that
        # confirmed one across several turns rather than in one) gets a
        # chance to become a real, verified booking - never on an
        # unconfirmed extraction, same trust bar as everything else here.
        if confident:
            appointment = await try_book_from_commitment(
                db,
                business_id=message.business_id,
                customer_id=message.customer_id,
                kind=found.kind,
                description=found.description,
                due_at=found.due_at,
                due_at_explicit=found.due_at_explicit,
                source_message_ids=found.source_message_ids,
            )
            if appointment is not None:
                commitment.status = CommitmentStatus.met
                commitment.resolved_at = datetime.now(timezone.utc)
                doctor_obj = await db.get(Doctor, appointment.doctor_id)
                if business and doctor_obj:
                    await notify.send_confirmation(
                        db, business=business, customer=customer,
                        doctor=doctor_obj, starts_at=appointment.starts_at,
                    )

    message.analysed_at = datetime.now(timezone.utc)

    if stored or extraction.rejected:
        logger.info(
            "analysed message=%s stored=%s rejected=%s cost=%sp",
            message_id,
            stored,
            extraction.rejected,
            extraction.cost_paise,
        )
    return stored


async def analyse_business(business_id: uuid.UUID, db: AsyncSession) -> int:
    """
    Catch what the real-time trigger never got to.

    ingest() queues analyse_message the instant an inbound message arrives -
    this exists for the two ways that can still miss one: a message ingested
    with enqueue_analysis=False (a Gmail backfill, private-customer messages
    excepted on purpose), or one whose real-time job failed every retry and
    was never requeued. A nightly per-business sweep is the backstop, not
    the primary path - which is why compress_profiles runs an hour after
    this, per scheduler.py: commitments have to exist before a profile can
    mention them.

    Re-enqueues onto analyse_message rather than analysing inline, so this
    stays a fast, lightweight fan-out and the real extraction work is still
    done by (and billed through) the one place that already does it.
    """
    cutoff = datetime.now(timezone.utc) - BACKFILL_WINDOW
    result = await db.execute(
        select(Message.id).where(
            Message.business_id == business_id,
            Message.direction == Direction.inbound,
            Message.analysed_at.is_(None),
            Message.occurred_at >= cutoff,
        )
    )
    message_ids = result.scalars().all()
    for message_id in message_ids:
        await queue.enqueue(QUEUE, {"message_id": str(message_id)}, db)
    return len(message_ids)


async def _run_message_job(job: Job, db: AsyncSession) -> None:
    raw_id = (job.payload or {}).get("message_id")
    if not raw_id:
        await queue.fail(job, "job payload has no message_id", db)
        return
    try:
        await analyse_message(uuid.UUID(raw_id), db)
        await queue.complete(job, db)
    except Exception as exc:  # noqa: BLE001 - the queue decides what to do next
        await queue.fail(job, f"{type(exc).__name__}: {exc}", db)


async def _run_business_job(job: Job, db: AsyncSession) -> None:
    raw_id = (job.payload or {}).get("business_id")
    if not raw_id:
        await queue.fail(job, "job payload has no business_id", db)
        return
    try:
        requeued = await analyse_business(uuid.UUID(raw_id), db)
        if requeued:
            logger.info("business sweep %s requeued %s message(s)", raw_id, requeued)
        await queue.complete(job, db)
    except Exception as exc:  # noqa: BLE001 - the queue decides what to do next
        await queue.fail(job, f"{type(exc).__name__}: {exc}", db)


async def run(*, stop: asyncio.Event | None = None) -> None:
    """
    Both queues, one process: analyse_business only ever fans out into
    analyse_message, so there is nothing gained by running it as a fourth
    separate deployable - it is genuinely the same "analysis" concern.
    """
    stop = stop or asyncio.Event()
    await asyncio.gather(
        run_worker(QUEUE, _run_message_job, worker_name="analyse_message", stop=stop),
        run_worker(BUSINESS_QUEUE, _run_business_job, worker_name="analyse_business", stop=stop),
    )


if __name__ == "__main__":
    import signal

    _stop = asyncio.Event()

    def _handle_signal(*_: object) -> None:
        logger.info("shutdown requested, finishing current job")
        _stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, AttributeError):
            pass

    asyncio.run(run(stop=_stop))
