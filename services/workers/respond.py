"""
Drafting a reply to an inbound message.

Runs after ingest, on its own queue. Deliberately separate from analysis:
extraction can take its time, but a reply is time-sensitive, and a customer
waiting behind a nightly job is a customer who has gone elsewhere.

What happens to the draft depends on the business's autonomy setting, and
that setting is the whole human-in-the-loop promise:

  observe   nothing is drafted at all
  draft     drafted and queued for a person to approve
  act       drafted and sent, with the draft kept as the record

Default is `draft`. Anything else has to be chosen deliberately by the owner,
because sending on someone's behalf is not a default anyone should inherit.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.ai import agent as agent_module
from shared.ai import context as agent_context
from shared.ai.client import AIError
from shared.billing import usage
from shared.channels.whatsapp.client import SERVICE_WINDOW
from shared.db.models import (
    Appointment,
    Business,
    Customer,
    Direction,
    Doctor,
    DraftAction,
    DraftStatus,
    IntakeChannel,
    Job,
    Message,
    MessageDraft,
    UsageEventType,
)
from shared.db import queue
from shared.db.worker_runner import run_worker_process
from shared.scheduling import availability as scheduling_availability
from shared.scheduling import booking as scheduling_booking
from shared.scheduling import notify
from shared.scheduling.booking import SlotUnavailable
from shared.utils.logging import get_logger

logger = get_logger(__name__)

QUEUE = "draft_reply"


async def _try_book(
    proposal: agent_module.Draft,
    *,
    business: Business,
    customer: Customer,
    message: Message,
    db: AsyncSession,
) -> Appointment | None:
    """
    Turn a model's booking decision into a real Appointment.

    Returns the Appointment if it actually happened, so the caller can send
    a confirmation - None covers every way this can go wrong (an
    unparseable time, a doctor name that does not match, the slot having
    been taken in the gap between the model reading its context and this
    running), and the caller's job is the same regardless of which: do not
    send a confirmation for a booking that did not happen.
    """
    if not proposal.book_slot:
        return None

    try:
        requested = datetime.fromisoformat(proposal.book_slot)
    except ValueError:
        logger.warning("agent returned unparseable book_slot %r", proposal.book_slot)
        return None

    doctors = (
        await db.execute(
            select(Doctor).where(Doctor.business_id == business.id, Doctor.active == True)  # noqa: E712
        )
    ).scalars().all()
    if not doctors:
        return None
    doctor = doctors[0]
    if len(doctors) > 1:
        if not proposal.book_doctor:
            logger.warning("book_slot set with no book_doctor across %d doctors", len(doctors))
            return None
        wanted = proposal.book_doctor.strip().lower()
        matches = [d for d in doctors if d.name.strip().lower() == wanted]
        if not matches:
            logger.warning("book_doctor %r matched no active doctor", proposal.book_doctor)
            return None
        doctor = matches[0]

    # open_slots() is re-run here, not trusted from whenever the context was
    # built - it is the single source of truth for "still free", and this
    # doubles as the check that the model did not invent a time.
    same_day = await scheduling_availability.open_slots(
        db, business=business, doctor=doctor, on_date=requested.date()
    )
    slot = next((s for s in same_day if s.starts_at == requested), None)
    if slot is None:
        logger.info("book_slot %s no longer open for doctor=%s", proposal.book_slot, doctor.id)
        return None

    try:
        return await scheduling_booking.book(
            db,
            business_id=business.id,
            doctor=doctor,
            customer=customer,
            slot=slot,
            intake_channel=IntakeChannel.whatsapp,
            source_message_ids=[message.id],
        )
    except SlotUnavailable:
        return None


async def draft_for_message(message_id: uuid.UUID, db: AsyncSession) -> MessageDraft | None:
    """
    Read the conversation this message belongs to and propose a reply.

    Returns None when nothing should be drafted - which is common and correct.
    """
    message = await db.get(Message, message_id)
    if message is None:
        return None

    # Only inbound messages get a reply. Drafting a response to our own
    # outbound message would have the agent talking to itself.
    if message.direction != Direction.inbound:
        return None

    # A phone call already got its reply, spoken live by the voice pipeline
    # the instant the caller finished talking - it cannot pause mid-call for
    # a human to approve a draft. Queuing one here would leave a stale
    # "pending" card in the approvals screen for a conversation that is
    # already over.
    channel = message.channel.value if hasattr(message.channel, "value") else message.channel
    if channel == "voice":
        return None

    customer = await db.get(Customer, message.customer_id)
    if customer is None or customer.is_private:
        # A private customer is never read by the agent and never answered.
        return None

    business = await db.get(Business, message.business_id)
    if business is None or not business.is_active:
        return None

    autonomy = (business.autonomy or "observe").lower()
    if autonomy == "observe":
        # Watching only. The business has not asked for drafts yet.
        return None

    # If a newer inbound message exists, this one is stale - the customer has
    # moved on, and answering the older message would be answering the wrong
    # question.
    newer = await db.execute(
        select(Message.id)
        .where(
            Message.customer_id == message.customer_id,
            Message.direction == Direction.inbound,
            Message.occurred_at > message.occurred_at,
        )
        .limit(1)
    )
    if newer.scalars().first() is not None:
        logger.debug("message %s superseded before drafting", message_id)
        return None

    # Supersede anything still pending for this customer, for the same reason.
    pending = await db.execute(
        select(MessageDraft).where(
            MessageDraft.customer_id == message.customer_id,
            MessageDraft.status == DraftStatus.pending,
        )
    )
    for stale in pending.scalars().all():
        stale.status = DraftStatus.superseded

    context = await agent_context.build(message.business_id, message.customer_id, db)

    try:
        proposal = await agent_module.draft_reply(context)
    except AIError:
        # Let the queue retry with backoff rather than losing the reply.
        raise

    # Metered here, before the no_action early-return below: a no_action
    # decision is still one real Claude call that cost real tokens, even
    # though nothing gets shown to the business - the usage happened
    # regardless of what the agent decided to do with it.
    usage.record(
        business_id=message.business_id,
        event_type=UsageEventType.ai_reply_generated,
        channel=channel,
        quantity=1,
        unit="call",
        krova_cost_paise=proposal.cost_paise,
        source_type="message",
        source_id=message.id,
        db=db,
    )

    if proposal.action == "no_action":
        logger.debug("agent chose no_action for message %s", message_id)
        return None

    if proposal.book_slot:
        appointment = await _try_book(proposal, business=business, customer=customer, message=message, db=db)
        if appointment is None:
            # The drafted message may already promise a time that did not
            # actually get reserved - sending it would be exactly the kind
            # of invented fact the whole agent is built to avoid. Escalating
            # rather than silently stripping book_slot and sending the
            # original text anyway: a person should pick the next slot with
            # the customer, not have this fail invisibly.
            logger.info("book_slot %s did not book, escalating instead", proposal.book_slot)
            proposal.action = "escalate"
            proposal.message = None
            proposal.gap = f"Could not book {proposal.book_slot} - offer the customer the next open slot"
        else:
            doctor = await db.get(Doctor, appointment.doctor_id)
            if doctor is not None:
                await notify.send_confirmation(
                    db, business=business, customer=customer,
                    doctor=doctor, starts_at=appointment.starts_at,
                )

    draft = MessageDraft(
        business_id=message.business_id,
        customer_id=message.customer_id,
        in_reply_to_id=message.id,
        channel=channel,
        action=(
            DraftAction.reply if proposal.action == "reply" else DraftAction.escalate
        ),
        status=DraftStatus.pending,
        body=proposal.message,
        reasoning=proposal.reasoning,
        gap=proposal.gap,
        confidence=proposal.confidence,
        used_context=proposal.context_message_ids,
        # A free-form reply only delivers inside the window, so a draft that
        # outlives it is worse than none - it would be approved and then fail.
        expires_at=message.occurred_at + SERVICE_WINDOW,
        cost_paise=proposal.cost_paise,
    )
    db.add(draft)
    await db.flush()

    if proposal.action == "escalate" and proposal.gap:
        await agent_module.record_gap(message.business_id, proposal.gap, db)

    logger.info(
        "drafted %s for business=%s customer=%s confidence=%.2f",
        proposal.action,
        message.business_id,
        message.customer_id,
        proposal.confidence,
    )
    return draft


async def expire_stale_drafts(db: AsyncSession) -> int:
    """
    Retire drafts whose window has closed.

    Approving one after expiry would send a message Meta refuses, so they are
    taken off the queue rather than left to fail in front of a person.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(MessageDraft).where(
            MessageDraft.status == DraftStatus.pending,
            MessageDraft.expires_at < now,
        )
    )
    stale = result.scalars().all()
    for draft in stale:
        draft.status = DraftStatus.expired
    return len(stale)


async def _run_job(job: Job, db: AsyncSession) -> None:
    raw_id = (job.payload or {}).get("message_id")
    if not raw_id:
        await queue.fail(job, "job payload has no message_id", db)
        return
    try:
        await draft_for_message(uuid.UUID(raw_id), db)
        await queue.complete(job, db)
    except Exception as exc:  # noqa: BLE001 - the queue decides what to do next
        await queue.fail(job, f"{type(exc).__name__}: {exc}", db)


if __name__ == "__main__":
    run_worker_process(QUEUE, _run_job, worker_name="draft_reply")
