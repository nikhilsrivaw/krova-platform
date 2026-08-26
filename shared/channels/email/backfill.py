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
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.channels import ingest
from shared.channels.email import gmail
from shared.db import queue
from shared.db.models import (
    Channel,
    ChannelConnection,
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
