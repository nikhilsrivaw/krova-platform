"""
Watching a business's voice escalation rate, instead of only being able to
look it up.

Call.escalated is already real and reliable per call (shared/ai/call_summary.py
writes it after every analysed call) - nothing aggregates a rate from it or
tells anyone when it's high. A business owner who never opens a dashboard
finds out the same way every competitor's customer does: complaints pile up
and nobody connects them to "the AI keeps giving up."

Same shape as shared/channels/whatsapp/health_monitor.py on purpose: alerts
only on a transition, never on every poll. The previous state is kept on
Business.settings (JSONB) so "still high" does not create a new Insight
every run - only the change that matters: normal -> high, or high -> normal.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Business, Call, Channel, ChannelConnection, ConnectionStatus, Insight
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Below this many calls in the window, a rate is noise, not a signal - five
# calls with one escalation is "100%" and means nothing.
_MIN_CALLS_FOR_SIGNAL = 5
_WINDOW = timedelta(days=7)
_RATE_THRESHOLD = 0.3
_SETTINGS_KEY = "escalation_alert"


async def _business_ids_with_voice(db: AsyncSession) -> list:
    result = await db.execute(
        select(ChannelConnection.business_id)
        .where(
            ChannelConnection.channel == Channel.voice,
            ChannelConnection.status == ConnectionStatus.active,
        )
        .distinct()
    )
    return list(result.scalars().all())


async def check_business(business_id, db: AsyncSession) -> None:
    business = await db.get(Business, business_id)
    if business is None:
        return

    since = datetime.now(timezone.utc) - _WINDOW
    total = (
        await db.execute(
            select(func.count(Call.id)).where(Call.business_id == business_id, Call.started_at >= since)
        )
    ).scalar_one()
    escalated = (
        await db.execute(
            select(func.count(Call.id)).where(
                Call.business_id == business_id, Call.started_at >= since, Call.escalated.is_(True),
            )
        )
    ).scalar_one()

    previous = (business.settings or {}).get(_SETTINGS_KEY) or {}
    previously_high = bool(previous.get("over_threshold"))

    if total < _MIN_CALLS_FOR_SIGNAL:
        # Too few calls to say anything - leave the last real reading alone
        # rather than overwriting it with a meaningless one.
        return

    rate = escalated / total
    now_high = rate >= _RATE_THRESHOLD

    business.settings = {
        **(business.settings or {}),
        _SETTINGS_KEY: {
            "rate": round(rate, 3),
            "total_calls": total,
            "escalated_calls": escalated,
            "over_threshold": now_high,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    if now_high and not previously_high:
        severity = "critical" if rate >= _RATE_THRESHOLD * 2 else "warning"
        db.add(Insight(
            business_id=business_id,
            kind="escalation_rate",
            title=f"{round(rate * 100)}% of calls this week needed a human",
            body=(
                f"{escalated} of the last {total} calls (7 days) ended in an escalation - "
                f"above the {round(_RATE_THRESHOLD * 100)}% watch threshold. Worth checking "
                "what the agent keeps failing to handle before it becomes a pattern customers notice."
            ),
            severity=severity,
            created_at=datetime.now(timezone.utc),
        ))
        logger.warning(
            "escalation rate crossed threshold business=%s rate=%.2f calls=%s", business_id, rate, total,
        )
    elif not now_high and previously_high:
        db.add(Insight(
            business_id=business_id,
            kind="escalation_rate",
            title="Call escalation rate is back to normal",
            body=f"{escalated} of the last {total} calls (7 days) needed a human - back under {round(_RATE_THRESHOLD * 100)}%.",
            severity="info",
            created_at=datetime.now(timezone.utc),
        ))

    await db.flush()


async def check_all(db: AsyncSession) -> int:
    """Check every business with an active voice connection. Returns how many were checked."""
    business_ids = await _business_ids_with_voice(db)
    checked = 0
    for business_id in business_ids:
        try:
            await check_business(business_id, db)
            checked += 1
        except Exception:
            # One tenant's transient failure must never stop the rest of the
            # sweep - same reasoning as health_monitor.check_all.
            logger.exception("escalation rate check crashed business=%s", business_id)
    return checked
