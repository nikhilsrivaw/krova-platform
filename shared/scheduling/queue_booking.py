"""
Issuing an OPD token into an open shift.

The one function every entry point - staff check-in, the public kiosk, and
the voice/WhatsApp agent's book_token action - goes through, so "is this
shift actually open right now" and "what's the next real number" are
answered once, not reimplemented three times slightly differently. Mirrors
shared/scheduling/booking.py's own shape for the same reason that file's
book() does: a SAVEPOINT around the insert catches a genuine race (two
check-ins computing the same next number at once) without needing a
separate locking scheme, and the unique constraint on (business, date,
shift, number) is the real guarantee either way.
"""

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared import verticals
from shared.db.models import (
    Business,
    Customer,
    IntakeChannel,
    QueueEntry,
    QueueStatus,
    Shift,
    ShiftSession,
)
from shared.scheduling import notify
from shared.utils.logging import get_logger

logger = get_logger(__name__)

_MAX_ATTEMPTS = 2


class ShiftNotOpen(Exception):
    """No open ShiftSession for this business/shift/day - callers translate
    this however fits their surface (an HTTP 409 for a router, None for the
    agent's own "never raise, always None on legitimate failure" contract)."""


async def get_open_session(
    db: AsyncSession, *, business_id: uuid.UUID, shift: Shift, on_date: date
) -> ShiftSession | None:
    result = await db.execute(
        select(ShiftSession).where(
            ShiftSession.business_id == business_id,
            ShiftSession.shift == shift,
            ShiftSession.session_date == on_date,
            ShiftSession.closed_at.is_(None),
        )
    )
    return result.scalars().first()


async def open_shifts_today(db: AsyncSession, *, business_id: uuid.UUID) -> list[ShiftSession]:
    """Every shift open right now, for a status display (kiosk idle screen,
    staff dashboard, or the agent's own "which shifts can I offer" context)."""
    today = datetime.now(timezone.utc).date()
    result = await db.execute(
        select(ShiftSession).where(
            ShiftSession.business_id == business_id,
            ShiftSession.session_date == today,
            ShiftSession.closed_at.is_(None),
        )
    )
    return list(result.scalars().all())


async def open_shift_summary(
    db: AsyncSession, *, business_id: uuid.UUID
) -> list[tuple[Shift, int]]:
    """(shift, waiting_count) for every open shift - the one query the kiosk
    status endpoint and the agent's own context both read from, so a
    customer never sees a different "who's open" answer from two surfaces."""
    sessions = await open_shifts_today(db, business_id=business_id)
    summary: list[tuple[Shift, int]] = []
    for session in sessions:
        waiting = (
            await db.execute(
                select(QueueEntry).where(
                    QueueEntry.business_id == business_id,
                    QueueEntry.queue_date == session.session_date,
                    QueueEntry.shift == session.shift,
                    QueueEntry.status == QueueStatus.waiting,
                )
            )
        ).scalars().all()
        summary.append((session.shift, len(waiting)))
    return summary


async def issue_token(
    db: AsyncSession,
    *,
    business_id: uuid.UUID,
    shift: Shift,
    customer_id: uuid.UUID | None,
    doctor_id: uuid.UUID | None,
    intake_channel: IntakeChannel,
    source_message_ids: list[uuid.UUID] | None = None,
) -> QueueEntry:
    """
    Raises ShiftNotOpen rather than returning None - unlike the agent's own
    try_book_token_from_agent wrapper (which does return None for every
    legitimate failure, matching try_book_from_agent's contract), this
    lower-level function is shared with HTTP callers that want a real
    exception to translate into their own status code.
    """
    today = datetime.now(timezone.utc).date()
    session = await get_open_session(db, business_id=business_id, shift=shift, on_date=today)
    if session is None:
        raise ShiftNotOpen(f"No open {shift.value} shift for business {business_id}")

    entry: QueueEntry | None = None
    for attempt in range(_MAX_ATTEMPTS):
        current_max = (
            await db.execute(
                select(func.max(QueueEntry.queue_number)).where(
                    QueueEntry.business_id == business_id,
                    QueueEntry.queue_date == today,
                    QueueEntry.shift == shift,
                )
            )
        ).scalar_one_or_none()
        next_number = (current_max or 0) + 1

        candidate = QueueEntry(
            business_id=business_id,
            customer_id=customer_id,
            doctor_id=doctor_id,
            shift=shift,
            intake_channel=intake_channel,
            source_message_ids=source_message_ids,
            queue_date=today,
            queue_number=next_number,
            status=QueueStatus.waiting,
            checked_in_at=datetime.now(timezone.utc),
        )
        try:
            async with db.begin_nested():
                db.add(candidate)
                await db.flush()
        except IntegrityError:
            if attempt == _MAX_ATTEMPTS - 1:
                raise
            continue
        entry = candidate
        break

    assert entry is not None
    logger.info(
        "queue token issued id=%s business=%s shift=%s number=%s channel=%s",
        entry.id, business_id, shift.value, entry.queue_number, intake_channel.value,
    )

    if customer_id is not None:
        business = await db.get(Business, business_id)
        customer = await db.get(Customer, customer_id)
        if business is not None and customer is not None:
            try:
                await notify.send_queue_checkin(
                    db, business=business, customer=customer, queue_number=entry.queue_number
                )
            except Exception:
                # Never let a notification failure undo or block the token
                # itself - the patient already has a real place in line.
                logger.exception("queue check-in notification failed for entry=%s", entry.id)

    return entry


async def try_book_token_from_agent(
    db: AsyncSession,
    *,
    book_token: str | None,
    business: Business,
    customer: Customer,
    intake_channel: IntakeChannel,
    source_message_ids: list[uuid.UUID],
) -> QueueEntry | None:
    """
    Turn an agent's token-booking decision (book_token, from shared/ai/
    agent.py's REPLY_TOOL schema or SYSTEM_STREAM's BOOK_TOKEN= line) into a
    real QueueEntry, or None for every way this can legitimately fail: an
    unrecognised shift name, or no shift being open by the time this runs -
    identical contract to shared/scheduling/booking.py's
    try_book_from_agent, for the exact same reason (the caller's job is
    always "do not confirm a token that was not actually issued", never to
    special-case a parse failure or a race differently from a plain "no").

    Gated on the opd_queue capability here, in this one shared function,
    not in each of respond.py's and pipeline.py's call sites - same
    reasoning try_book_from_agent's own docstring gives for scheduling.
    """
    if not book_token:
        return None

    if not verticals.has_capability(business.vertical, "opd_queue"):
        logger.warning(
            "agent returned book_token for a non-opd_queue business=%s, ignoring",
            business.id,
        )
        return None

    try:
        shift = Shift(book_token.strip().lower())
    except ValueError:
        logger.warning("agent returned unrecognised book_token %r", book_token)
        return None

    if intake_channel != IntakeChannel.manual and not source_message_ids:
        raise ValueError(f"{intake_channel.value} token bookings must cite source_message_ids")

    try:
        return await issue_token(
            db,
            business_id=business.id,
            shift=shift,
            customer_id=customer.id,
            doctor_id=None,
            intake_channel=intake_channel,
            source_message_ids=source_message_ids,
        )
    except ShiftNotOpen:
        logger.info("book_token %s no longer open for business=%s", shift.value, business.id)
        return None
