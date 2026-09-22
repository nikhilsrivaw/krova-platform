"""
Reading an inbox's history.

The moment that sells this product. A business connects Gmail and, minutes
later, is told what they promised and what was promised to them over the last
three months - things they had forgotten, from people they had stopped
chasing. Nobody has to type anything, and nothing has to happen first.

WhatsApp cannot do this. Meta delivers messages only from the moment of
connection, so a WhatsApp-first competitor's first screen is necessarily
empty. This one is full on day one.

Two constraints shape the design.

Cost. An inbox is mostly newsletters, receipts and alerts. Reading all of it
would cost real money per customer and bury the few messages that matter, so
the query is narrow and the ceiling is hard.

Fairness to the mailbox owner. Only what a business genuinely exchanged with
its customers is read. The narrow query is a privacy decision as much as a
cost one.

A third thing this module owns, added later: staying current after that
first read. The one-time backfill above used to be the only thing that
ever touched a connected mailbox - ChannelConnection.backfilled_through
was written once and never read again anywhere in the codebase, so every
email after the initial connection was invisible to Krova unless someone
remembered to click "Backfill Now" in Settings. sync_all_active() below is
the ongoing half: a periodic sweep (see services/api/scheduler.py) that
re-runs this same read-dedupe-ingest logic on a short, cheap lookback for
every connected mailbox, unprompted - the same shape every other channel
(WhatsApp, Instagram, voice) already gets via webhook/live delivery.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt, encrypt
from shared.channels import ingest
from shared.channels.email import gmail
from shared.db import queue
from shared.db.models import (
    Channel,
    ChannelConnection,
    ConnectionStatus,
    Direction,
    IdentityKind,
    Message,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# How far back to read on first connection. Long enough that forgotten
# promises surface, short enough to stay cheap.
BACKFILL_DAYS = 90

# Hard ceiling per connection. A busy inbox could otherwise pull tens of
# thousands of messages on signup, at real cost, before anyone has decided
# Krova is worth paying for.
MAX_MESSAGES = 500

PAGE_SIZE = 100

# The ongoing sync's own, much smaller window - this runs every 30 minutes
# (see scheduler.py), so a few days of lookback is generous slack for a
# missed/delayed run, not a re-read of the mailbox's history. Re-scanning
# the overlap on every run is safe and cheap: run_backfill's own ingest()
# call dedupes on Message.external_id, so an already-stored message is
# just skipped, never duplicated.
SYNC_LOOKBACK_DAYS = 3
SYNC_MAX_MESSAGES = 100


@dataclass(slots=True)
class BackfillResult:
    fetched: int
    stored: int
    skipped: int
    customers_created: int
    oldest: datetime | None
    newest: datetime | None


async def run_backfill(
    connection: ChannelConnection,
    access_token: str,
    db: AsyncSession,
    *,
    days: int = BACKFILL_DAYS,
    max_messages: int = MAX_MESSAGES,
) -> BackfillResult:
    """
    Read recent history from a connected mailbox into the platform.

    Each message goes through the same ingest() every channel uses, so the
    commitment extractor, the customer timeline and cross-channel identity all
    cover email without knowing anything about email.
    """
    client = gmail.GmailClient(access_token)
    mailbox = connection.external_account_id

    query = gmail.backfill_query(days)
    fetched = stored = skipped = created = 0
    oldest: datetime | None = None
    newest: datetime | None = None
    page_token: str | None = None

    while fetched < max_messages:
        remaining = max_messages - fetched
        ids, page_token = await client.list_message_ids(
            query=query, page_token=page_token, limit=min(PAGE_SIZE, remaining)
        )
        if not ids:
            break

        for message_id in ids:
            fetched += 1
            try:
                raw = await client.get_message(message_id)
            except gmail.GmailError as exc:
                logger.warning("could not fetch %s: %s", message_id, exc)
                skipped += 1
                continue

            parsed = gmail.parse_message(raw, mailbox)
            if parsed is None:
                skipped += 1
                continue

            # Attribute the message to the other party, never to the mailbox
            # owner. On an outbound mail that is the recipient; on an inbound
            # one, the sender. Getting this backwards would file a business's
            # entire history under a single customer called "themselves".
            if parsed.is_outbound:
                counterparty = next(
                    (e for e in parsed.to_emails if e != mailbox.lower()), None
                )
            else:
                counterparty = parsed.from_email

            if not counterparty or gmail.is_machine_sender(counterparty):
                # Robots never promise anything. Skipping them here is the
                # difference between analysing a business's real conversations
                # and analysing its bank alerts.
                skipped += 1
                continue

            result = await ingest.ingest(
                business_id=connection.business_id,
                channel=Channel.email,
                direction=Direction.outbound if parsed.is_outbound else Direction.inbound,
                identity_kind=IdentityKind.email,
                identity_value=counterparty,
                external_id=parsed.external_id,
                text=parsed.body,
                subject=parsed.subject,
                occurred_at=parsed.occurred_at,
                display_name=None if parsed.is_outbound else parsed.from_name,
                connection_id=connection.id,
                raw=parsed.raw,
                db=db,
                # Analysis is queued once at the end rather than per message:
                # 500 separate jobs would each re-read the same conversations.
                enqueue_analysis=False,
            )

            if result.created:
                stored += 1
                if result.customer is not None and result.created:
                    created += 1
                oldest = min(oldest or parsed.occurred_at, parsed.occurred_at)
                newest = max(newest or parsed.occurred_at, parsed.occurred_at)
            else:
                skipped += 1

        if not page_token:
            break

    connection.backfilled_through = oldest
    logger.info(
        "gmail backfill mailbox=%s fetched=%s stored=%s skipped=%s",
        mailbox,
        fetched,
        stored,
        skipped,
    )
    return BackfillResult(
        fetched=fetched,
        stored=stored,
        skipped=skipped,
        customers_created=created,
        oldest=oldest,
        newest=newest,
    )


async def queue_analysis_for_backfill(
    business_id: uuid.UUID, db: AsyncSession, *, limit: int = 200
) -> int:
    """
    Queue analysis over what the backfill brought in.

    One job per customer, not per message. A promise lives in a conversation,
    and re-reading the same thread once per message would multiply the cost by
    the length of the thread for no extra signal.
    """
    # DISTINCT ON, not max(id): Postgres has no max() for uuid, and the newest
    # message is the one worth analysing anyway - the extractor reads the whole
    # conversation from whichever message it is given.
    result = await db.execute(
        select(Message.id)
        .distinct(Message.customer_id)
        .where(
            Message.business_id == business_id,
            Message.channel == Channel.email,
            Message.analysed_at.is_(None),
        )
        .order_by(Message.customer_id, Message.occurred_at.desc())
        .limit(limit)
    )

    queued = 0
    for message_id in result.scalars().all():
        await queue.enqueue("analyse_message", {"message_id": str(message_id)}, db)
        queued += 1

    logger.info("queued %s analysis jobs after backfill", queued)
    return queued


class NeedsReauth(Exception):
    """
    Raised when a Gmail connection's access token cannot be refreshed - no
    refresh_token was ever stored, or Google itself rejected the refresh
    (revoked/expired). The connection is marked needs_reauth either way,
    here, so both callers get that for free; each decides how to surface
    it - an HTTP 409 for a live request, a skipped connection for a sweep.
    """


async def usable_access_token(connection: ChannelConnection, db: AsyncSession) -> str:
    """
    A live Gmail access token for this connection, refreshing if the
    stored one has expired.

    Google's last an hour, so this refreshes constantly - unlike Meta's,
    which need a scheduled job because they last sixty days and fail
    silently. Shared by the manual "Backfill Now" endpoint
    (services/api/routers/gmail_channel.py) and sync_all_active() below,
    so the refresh logic exists once.
    """
    now = datetime.now(timezone.utc)
    if connection.token_expires_at and connection.token_expires_at > now + timedelta(minutes=2):
        return decrypt(connection.access_token)

    if not connection.refresh_token:
        connection.status = ConnectionStatus.needs_reauth
        raise NeedsReauth("Gmail access has expired. Please reconnect the mailbox.")

    try:
        tokens = await gmail.refresh_access_token(decrypt(connection.refresh_token))
    except gmail.GmailError as exc:
        connection.status = ConnectionStatus.needs_reauth
        raise NeedsReauth(str(exc)) from exc

    access_token = tokens["access_token"]
    connection.access_token = encrypt(access_token)
    connection.token_issued_at = now
    connection.token_expires_at = now + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    return access_token


async def sync_all_active(db: AsyncSession) -> int:
    """
    The ongoing half of reading a mailbox - see this module's own
    docstring for why this exists. Re-runs run_backfill on a short,
    cheap lookback for every connected Gmail mailbox, unprompted.

    A connection that needs re-auth, or a single Gmail API failure, is
    logged and skipped rather than aborting the whole sweep - one
    business's broken connection must never stop every other business's
    mail from syncing.

    Returns how many new messages were stored across every mailbox.
    """
    connections = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.channel == Channel.email,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().all()

    total_stored = 0
    for connection in connections:
        try:
            token = await usable_access_token(connection, db)
        except NeedsReauth as exc:
            logger.info(
                "gmail sync skipped, needs reauth mailbox=%s: %s",
                connection.external_account_id, exc,
            )
            continue

        try:
            outcome = await run_backfill(
                connection, token, db, days=SYNC_LOOKBACK_DAYS, max_messages=SYNC_MAX_MESSAGES,
            )
        except gmail.GmailError:
            logger.exception("gmail sync failed mailbox=%s", connection.external_account_id)
            continue

        if outcome.stored:
            total_stored += outcome.stored
            await queue_analysis_for_backfill(connection.business_id, db)

    if total_stored:
        logger.info("gmail sync stored %s new message(s) across %s mailbox(es)",
                     total_stored, len(connections))
    return total_stored
