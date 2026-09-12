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

Channel health, because a number's quality rating drops before Meta
restricts it, and restriction happens well before messages visibly stop -
by then the drop was days old and nobody was watching for it.

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


async def refresh_calendar_tokens() -> None:
    """
    Renew Google Calendar access tokens before they lapse - separate job
    from refresh_channel_tokens above (a different table, a different
    provider's token endpoint), not a branch added to that one.
    """
    from shared.db.session import AsyncSessionLocal
    from shared.integrations import google_calendar

    try:
        async with AsyncSessionLocal() as db:
            await google_calendar.refresh_expiring(db)
    except Exception:
        logger.exception("calendar token refresh job failed")


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


async def send_due_recalls() -> None:
    """
    Send every chronic-care recall reminder currently due - care_recall
    capability. See shared/scheduling/recall.py.
    """
    from shared.db.session import AsyncSessionLocal
    from shared.scheduling import recall

    try:
        async with AsyncSessionLocal() as db:
            sent = await recall.send_due_recalls(db)
            await db.commit()
            if sent:
                logger.info("sent %s chronic-care recall reminder(s)", sent)
    except Exception:
        logger.exception("recall reminder job failed")


async def send_review_requests() -> None:
    """
    Cross-vertical review-request sweep, gated per-business on
    Business.settings["google_review_url"]. See shared/scheduling/recall.py.
    """
    from shared.db.session import AsyncSessionLocal
    from shared.scheduling import recall

    try:
        async with AsyncSessionLocal() as db:
            sent = await recall.send_review_requests(db)
            await db.commit()
            if sent:
                logger.info("sent %s review request(s)", sent)
    except Exception:
        logger.exception("review request job failed")


async def escalate_unacknowledged() -> None:
    """
    The SMS failsafe for an Escalation nobody acknowledged in time. See
    shared/care/escalation_failsafe.py.
    """
    from shared.care import escalation_failsafe
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            processed = await escalation_failsafe.check_unacknowledged(db)
            await db.commit()
            if processed:
                logger.info("processed %s unacknowledged escalation(s)", processed)
    except Exception:
        logger.exception("escalation failsafe job failed")


async def send_cod_confirmations() -> None:
    """D2C, order_sync capability. See shared/scheduling/recall.py."""
    from shared.db.session import AsyncSessionLocal
    from shared.scheduling import recall

    try:
        async with AsyncSessionLocal() as db:
            sent = await recall.send_cod_confirmations(db)
            await db.commit()
            if sent:
                logger.info("sent %s COD confirmation(s)", sent)
    except Exception:
        logger.exception("COD confirmation job failed")


async def send_abandoned_cart_recovery() -> None:
    """D2C, order_sync capability. See shared/scheduling/recall.py."""
    from shared.db.session import AsyncSessionLocal
    from shared.scheduling import recall

    try:
        async with AsyncSessionLocal() as db:
            sent = await recall.send_abandoned_cart_recovery(db)
            await db.commit()
            if sent:
                logger.info("sent %s abandoned-cart recovery message(s)", sent)
    except Exception:
        logger.exception("abandoned cart recovery job failed")


async def check_cod_call_failsafe() -> None:
    """
    The voice-call failsafe for an unanswered COD WhatsApp confirmation,
    plus escalating any call that itself went unanswered - order_sync
    capability. See shared/care/cod_call_failsafe.py.
    """
    from shared.care import cod_call_failsafe
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            placed = await cod_call_failsafe.due_for_call(db)
            escalated = await cod_call_failsafe.escalate_no_outcome(db)
            await db.commit()
            if placed or escalated:
                logger.info(
                    "COD call failsafe: placed=%s escalated=%s", placed, escalated
                )
    except Exception:
        logger.exception("COD call failsafe job failed")


async def send_due_campaign_steps() -> None:
    """
    Send every campaign follow-up step that has become due - the drip/
    sequenced campaign feature. See shared/campaigns/sequencer.py; a
    campaign with no steps is untouched by this job.
    """
    from shared.campaigns import sequencer
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            sent = await sequencer.send_due_steps(db)
            await db.commit()
            if sent:
                logger.info("sent %s campaign follow-up step message(s)", sent)
    except Exception:
        logger.exception("campaign step sweep failed")


async def run_due_automation_steps() -> None:
    """
    Fire every AutomationStep whose configured delay has elapsed - the
    "wait, then do this" half of the automations engine (phase 3). See
    shared/care/post_call_actions.py::run_due_steps; a rule with no
    delayed step is untouched by this job.
    """
    from shared.care import post_call_actions
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            ran = await post_call_actions.run_due_steps(db)
            await db.commit()
            if ran:
                logger.info("ran %s delayed automation step(s)", ran)
    except Exception:
        logger.exception("delayed automation step sweep failed")


async def send_repeat_purchase_nudges() -> None:
    """D2C, order_sync capability. See shared/scheduling/recall.py."""
    from shared.db.session import AsyncSessionLocal
    from shared.scheduling import recall

    try:
        async with AsyncSessionLocal() as db:
            sent = await recall.send_repeat_purchase_nudges(db)
            await db.commit()
            if sent:
                logger.info("sent %s repeat-purchase nudge(s)", sent)
    except Exception:
        logger.exception("repeat-purchase nudge job failed")


async def check_intent_leakage() -> None:
    """D2C, order_sync capability. See shared/care/intent_leakage.py."""
    from shared.care import intent_leakage
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            created = await intent_leakage.check_intent_leakage(db)
            await db.commit()
            if created:
                logger.info("created %s intent-leakage insight(s)", created)
    except Exception:
        logger.exception("intent leakage sweep failed")


async def sync_shiprocket() -> None:
    """D2C, order_sync capability. See shared/care/shiprocket_sync.py."""
    from shared.care import shiprocket_sync
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            processed = await shiprocket_sync.sync_all(db)
            await db.commit()
            if processed:
                logger.info("processed %s Shiprocket NDR event(s)", processed)
    except Exception:
        logger.exception("Shiprocket sync job failed")


async def check_deadline_calls() -> None:
    """
    Place one proactive voice call per commitment approaching its due
    date, opt-in per business - cross-vertical, not gated on any
    capability the way check_clinic_commitments below is.
    See shared/care/commitment_deadline_calls.py.
    """
    from shared.care import commitment_deadline_calls
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            placed = await commitment_deadline_calls.check_deadline_calls(db)
            await db.commit()
            if placed:
                logger.info("placed %s proactive deadline call(s)", placed)
    except Exception:
        logger.exception("deadline call sweep failed")


async def check_clinic_commitments() -> None:
    """
    Scan overdue commitments for clinic businesses and surface them on the
    daily briefing - care_recall capability. See shared/ai/recall_insights.py.
    """
    from shared.ai import recall_insights
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            created = await recall_insights.check_clinic_commitments(db)
            await db.commit()
            if created:
                logger.info("created %s clinic overdue-commitment insight(s)", created)
    except Exception:
        logger.exception("clinic commitment sweep failed")


async def check_ecommerce_commitments() -> None:
    """
    Scan overdue promised-refund/replacement commitments for ecommerce
    businesses and surface them on the daily briefing - order_sync
    capability. See shared/ai/recall_insights.py.
    """
    from shared.ai import recall_insights
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            created = await recall_insights.check_ecommerce_commitments(db)
            await db.commit()
            if created:
                logger.info("created %s ecommerce overdue-refund insight(s)", created)
    except Exception:
        logger.exception("ecommerce commitment sweep failed")


async def check_rto_risk_pincodes() -> None:
    """D2C, order_sync capability. See shared/care/intent_leakage.py."""
    from shared.care import intent_leakage
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            created = await intent_leakage.check_rto_risk_pincodes(db)
            await db.commit()
            if created:
                logger.info("created %s RTO-risk insight(s)", created)
    except Exception:
        logger.exception("RTO-risk pincode sweep failed")


async def send_onboarding_dropoff_nudges() -> None:
    """
    One nudge for a customer whose business reported a trial_started
    lifecycle event with no later activated one, past the 3-day window
    research found most predictive - product_feedback capability. See
    shared/care/onboarding_dropoff.py.
    """
    from shared.care import onboarding_dropoff
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            sent = await onboarding_dropoff.send_onboarding_dropoff_nudges(db)
            await db.commit()
            if sent:
                logger.info("sent %s onboarding-dropoff nudge(s)", sent)
    except Exception:
        logger.exception("onboarding dropoff nudge job failed")


async def send_expansion_nudges() -> None:
    """
    One nudge per not-yet-nudged usage-milestone lifecycle event -
    product_feedback capability. See shared/care/expansion_signals.py.
    """
    from shared.care import expansion_signals
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            sent = await expansion_signals.send_expansion_nudges(db)
            await db.commit()
            if sent:
                logger.info("sent %s expansion nudge(s)", sent)
    except Exception:
        logger.exception("expansion nudge job failed")


async def check_feature_request_dedup() -> None:
    """
    Merge newly-created feature_request Insights into an existing open
    one when they're the same underlying ask - product_feedback
    capability. See shared/care/feature_request_dedup.py.
    """
    from shared.care import feature_request_dedup
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            merged = await feature_request_dedup.check_feature_request_dedup(db)
            await db.commit()
            if merged:
                logger.info("merged %s duplicate feature request(s)", merged)
    except Exception:
        logger.exception("feature request dedup sweep failed")


async def check_channel_health() -> None:
    """
    Watch quality rating and sending eligibility instead of only being able
    to look them up - see shared/channels/whatsapp/health_monitor.py for why
    this alerts only on a transition, never on every poll.
    """
    from shared.channels.whatsapp import health_monitor
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            checked = await health_monitor.check_all(db)
            await db.commit()
            logger.info("checked health of %s WhatsApp connection(s)", checked)
    except Exception:
        logger.exception("channel health check job failed")


async def check_escalation_rate() -> None:
    """
    Watch each business's voice escalation rate instead of only being able
    to look it up - see shared/care/escalation_alerts.py for why this
    alerts only on a transition, never on every poll.
    """
    from shared.care import escalation_alerts
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            checked = await escalation_alerts.check_all(db)
            await db.commit()
            logger.info("checked escalation rate for %s business(es)", checked)
    except Exception:
        logger.exception("escalation rate check job failed")


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
        refresh_calendar_tokens,
        CronTrigger(hour=3, minute=45, timezone=IST),
        id="refresh_calendar_tokens",
        replace_existing=True,
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
        send_due_recalls,
        IntervalTrigger(minutes=30),
        id="send_due_recalls",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    scheduler.add_job(
        send_review_requests,
        IntervalTrigger(minutes=30),
        id="send_review_requests",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    scheduler.add_job(
        escalate_unacknowledged,
        IntervalTrigger(minutes=5),
        id="escalate_unacknowledged",
        replace_existing=True,
        misfire_grace_time=300,
    )

    scheduler.add_job(
        send_cod_confirmations,
        IntervalTrigger(minutes=15),
        id="send_cod_confirmations",
        replace_existing=True,
        misfire_grace_time=900,
    )

    scheduler.add_job(
        send_abandoned_cart_recovery,
        IntervalTrigger(minutes=30),
        id="send_abandoned_cart_recovery",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    # Delays are in whole days, so checking every 30 minutes is plenty of
    # resolution - same cadence as the other reminder-style sweeps above.
    scheduler.add_job(
        send_due_campaign_steps,
        IntervalTrigger(minutes=30),
        id="send_due_campaign_steps",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    scheduler.add_job(
        send_repeat_purchase_nudges,
        IntervalTrigger(hours=6),
        id="send_repeat_purchase_nudges",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # After send_cod_confirmations has had time to go unanswered.
    scheduler.add_job(
        check_cod_call_failsafe,
        IntervalTrigger(minutes=30),
        id="check_cod_call_failsafe",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    scheduler.add_job(
        check_deadline_calls,
        IntervalTrigger(minutes=30),
        id="check_deadline_calls",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    scheduler.add_job(
        check_intent_leakage,
        CronTrigger(hour=7, minute=30, timezone=IST),
        id="check_intent_leakage",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        sync_shiprocket,
        IntervalTrigger(minutes=20),
        id="sync_shiprocket",
        replace_existing=True,
        misfire_grace_time=1200,
    )

    # Morning run, after the nightly analysis/profile passes so the day's
    # briefing reflects last night's extraction - not wired to run before
    # commitments exist to scan, same ordering reasoning as compress_profiles.
    scheduler.add_job(
        check_clinic_commitments,
        CronTrigger(hour=7, minute=0, timezone=IST),
        id="check_clinic_commitments",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Same morning slot as check_clinic_commitments - after the nightly
    # analysis pass, for the identical reason.
    scheduler.add_job(
        check_ecommerce_commitments,
        CronTrigger(hour=7, minute=15, timezone=IST),
        id="check_ecommerce_commitments",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        check_rto_risk_pincodes,
        CronTrigger(hour=7, minute=45, timezone=IST),
        id="check_rto_risk_pincodes",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        check_feature_request_dedup,
        IntervalTrigger(minutes=30),
        id="check_feature_request_dedup",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    scheduler.add_job(
        send_onboarding_dropoff_nudges,
        IntervalTrigger(hours=6),
        id="send_onboarding_dropoff_nudges",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        send_expansion_nudges,
        IntervalTrigger(minutes=30),
        id="send_expansion_nudges",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    scheduler.add_job(
        check_channel_health,
        IntervalTrigger(hours=4),
        id="check_channel_health",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # A tighter interval than every other sweep here on purpose - a
    # business-configured delay is meant to feel like "N minutes later",
    # not "sometime in the next half hour". The query itself is a single
    # indexed lookup, empty most runs, so a minute is cheap to poll.
    scheduler.add_job(
        run_due_automation_steps,
        IntervalTrigger(minutes=1),
        id="run_due_automation_steps",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # A daily read is enough - the underlying window is 7 days, so this
    # never needs to notice a change faster than once a day.
    scheduler.add_job(
        check_escalation_rate,
        CronTrigger(hour=8, minute=0, timezone=IST),
        id="check_escalation_rate",
        replace_existing=True,
        misfire_grace_time=3600,
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
