"""
Scheduled work.

Each job exists because of something that fails quietly.

Token refresh, because Embedded Signup tokens expire after 60 days and a
client whose token lapses simply stops receiving messages. No error reaches
us and none reaches them.

Stalled job reclaim, because a worker that dies holding a job leaves it
'running' forever. Without this, one crash silently strands whatever that
worker was processing.

Draft expiry, because approving a draft after its service window closed
sends a message the channel refuses - better to retire it quietly than let
a person approve something that is about to fail.

Nightly analysis, because the cold path is what makes the live agent fast -
it compresses a customer's history into something the hot path can afford to
read.

Run inside the API process rather than as a separate service. At this scale
another deployable costs more in operations than it saves, and each job is
short. It moves out when there is more than one API instance, because these
must not run twice.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from shared.utils.logging import get_logger

logger = get_logger(__name__)

IST = "Asia/Kolkata"


async def refresh_channel_tokens() -> None:
    """Renew credentials before they lapse."""
    from shared.channels.whatsapp import token_refresh
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            await token_refresh.refresh_expiring(db)
    except Exception:
        logger.exception("token refresh job failed")


async def reclaim_stalled_jobs() -> None:
    """Return work held by a worker that died."""
    from shared.db import queue
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            reclaimed = await queue.reclaim_stalled(db)
            await db.commit()
            if reclaimed:
                logger.warning("reclaimed %s stalled job(s)", reclaimed)
    except Exception:
        logger.exception("stalled job reclaim failed")


async def compress_profiles() -> None:
    """
    Rewrite customer profiles for conversations that moved today.

    The job that decides whether the live agent answers in under a second or
    reads two hundred raw messages first.
    """
    from sqlalchemy import select

    from services.workers import profile
    from shared.db.models import Business
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Business.id).where(Business.is_active == True)  # noqa: E712
            )
            total = 0
            for business_id in result.scalars().all():
                total += await profile.queue_stale(business_id, db)
            await db.commit()
            logger.info("queued %s customer profiles for compression", total)
    except Exception:
        logger.exception("profile compression trigger failed")


async def expire_stale_drafts() -> None:
    """
    Retire drafts whose service window closed before anyone approved them.

    Not wired to anything before this - written, exposed, never actually
    called. A stale pending draft sitting in the approvals queue would be
    approved and then fail against a window Meta has already closed.
    """
    from services.workers import respond
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            expired = await respond.expire_stale_drafts(db)
            await db.commit()
            if expired:
                logger.info("expired %s stale draft(s)", expired)
    except Exception:
        logger.exception("draft expiry job failed")


async def send_appointment_reminders() -> None:
    """
    Send every 24-hour and 2-hour appointment reminder currently due.

    The single highest-leverage thing the Scheduling capability does after
    booking itself - see shared/scheduling/reminders.py.
    """
    from shared.db.session import AsyncSessionLocal
    from shared.scheduling import reminders

    try:
        async with AsyncSessionLocal() as db:
            sent = await reminders.send_due_reminders(db)
            await db.commit()
            if sent:
                logger.info("sent %s appointment reminder(s)", sent)
    except Exception:
        logger.exception("appointment reminder job failed")


async def nightly_analysis() -> None:
    """
    Queue a re-read of every business's recent conversations.

    Runs late so the compression is ready before the next working day - the
    live agent reads what this produced, not the raw history.
    """
    from sqlalchemy import select

    from shared.db import queue
    from shared.db.models import Business
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Business.id).where(Business.is_active == True)  # noqa: E712
            )
            queued = 0
            for business_id in result.scalars().all():
                await queue.enqueue(
                    "analyse_business", {"business_id": str(business_id)}, db
                )
                queued += 1
            await db.commit()
            logger.info("nightly analysis queued for %s businesses", queued)
    except Exception:
        logger.exception("nightly analysis trigger failed")


def build() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=IST)

    # Daily, in the small hours. Tokens have ten days of headroom, so the exact
    # time does not matter - only that it happens every day without fail.
    scheduler.add_job(
        refresh_channel_tokens,
        CronTrigger(hour=3, minute=30, timezone=IST),
        id="refresh_channel_tokens",
        replace_existing=True,
        # If the server was down at 3:30, still run when it comes back.
        misfire_grace_time=6 * 3600,
    )

    scheduler.add_job(
        reclaim_stalled_jobs,
        IntervalTrigger(minutes=5),
        id="reclaim_stalled_jobs",
        replace_existing=True,
        misfire_grace_time=300,
    )

    scheduler.add_job(
        expire_stale_drafts,
        IntervalTrigger(minutes=15),
        id="expire_stale_drafts",
        replace_existing=True,
        misfire_grace_time=900,
    )

    scheduler.add_job(
        send_appointment_reminders,
        IntervalTrigger(minutes=15),
        id="send_appointment_reminders",
        replace_existing=True,
        misfire_grace_time=900,
    )

    scheduler.add_job(
        nightly_analysis,
        CronTrigger(hour=22, minute=0, timezone=IST),
        id="nightly_analysis",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # After the analysis pass: commitments must exist before a profile can
    # mention them.
    scheduler.add_job(
        compress_profiles,
        CronTrigger(hour=23, minute=0, timezone=IST),
        id="compress_profiles",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    return scheduler
