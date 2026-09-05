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
from shared.channels.send_draft import DraftSendError, send_draft
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
    QueueEntry,
    UsageEventType,
)
from shared.db import queue
from shared.db.worker_runner import run_worker_process
from shared.scheduling import booking as scheduling_booking
from shared.scheduling import notify
from shared.scheduling import queue_booking
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

    Thin wrapper over shared/scheduling/booking.py's try_book_from_agent -
    the actual matching/booking logic is shared with a live call's booking
    path now (pipeline.py), not duplicated here. This wrapper exists only
    to unpack a Draft and supply this path's own intake_channel/
    source_message_ids.
    """
    return await scheduling_booking.try_book_from_agent(
        db,
        book_slot=proposal.book_slot,
        book_doctor=proposal.book_doctor,
        book_property=proposal.book_property,
        business=business,
        customer=customer,
        intake_channel=IntakeChannel.whatsapp,
        source_message_ids=[message.id],
    )


async def _try_book_token(
    proposal: agent_module.Draft,
    *,
    business: Business,
    customer: Customer,
    message: Message,
    db: AsyncSession,
) -> QueueEntry | None:
    """
    Turn a model's token-booking decision into a real QueueEntry. Same thin-
    wrapper shape as _try_book above, over shared/scheduling/
    queue_booking.py's try_book_token_from_agent - shared with the voice
    pipeline's booking path, not duplicated here.
    """
    return await queue_booking.try_book_token_from_agent(
        db,
        book_token=proposal.book_token,
        business=business,
        customer=customer,
        intake_channel=IntakeChannel.whatsapp,
        source_message_ids=[message.id],
    )


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

    if proposal.book_token:
        queue_entry = await _try_book_token(proposal, business=business, customer=customer, message=message, db=db)
        if queue_entry is None:
            # Same reasoning as the book_slot branch above: a drafted
            # message may already promise a token that was not actually
            # issued (no shift open, or an unrecognised shift name) -
            # escalate rather than send an invented queue position.
            logger.info("book_token %s did not book, escalating instead", proposal.book_token)
            proposal.action = "escalate"
            proposal.message = None
            proposal.gap = f"Could not add customer to the {proposal.book_token} queue - no shift open, or check the shift name"
        # No separate confirmation send here - queue_booking.issue_token
        # already fires notify.send_queue_checkin itself on success, unlike
        # scheduling's book() which leaves the confirmation to the caller.

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

    # `act` sends without a person - the whole point of the setting - but
    # only for a genuine reply. escalate exists precisely because the agent
    # is unsure what to say; act mode does not override that judgment, it
    # only skips the human step for replies the agent was confident enough
    # to propose in the first place. WhatsApp only for now - send_draft
    # (shared/channels/send_draft.py) has no Instagram path yet, and a
    # channel != whatsapp draft is left pending for a person either way.
    if autonomy == "act" and proposal.action == "reply" and channel == "whatsapp":
        try:
            await send_draft(draft, message.business_id, db, reviewed_by_user_id=None)
        except DraftSendError as exc:
            # Left pending rather than failed - a person can still approve
            # it manually, which is strictly better than losing the reply
            # because act mode's extra send attempt didn't work this time.
            logger.warning(
                "act-mode auto-send failed for draft=%s, left pending: %s",
                draft.id, exc,
            )

    logger.info(
        "drafted %s for business=%s customer=%s confidence=%.2f autonomy=%s status=%s",
        proposal.action,
        message.business_id,
        message.customer_id,
        proposal.confidence,
        autonomy,
        draft.status.value,
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
