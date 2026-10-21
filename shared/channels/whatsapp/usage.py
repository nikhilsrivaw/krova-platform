"""
What a business's WhatsApp messages cost it, as Meta reports it - so the owner can see it in KROVA.

Since 1 October 2026 Meta charges again for replies inside the 24-hour window (service messages, after 1,000 free
per number per month) and for utility templates sent there. Meta bills the business's own WhatsApp account, never
KROVA. A WhatsApp account with no working payment method stops delivering those messages, and the only sign KROVA
gets is a failed status with error 131042 - so that is remembered on the connection and shown loudly.

Counted from the status webhook: each message's "sent" status carries `pricing` - `category` (service, utility,
marketing, authentication ...) and `type` ("regular" = charged; "free_customer_service", "free_entry_point" ... =
free). Meta is deprecating the `billable` flag in favour of these two, so it is not used. Only "sent" is counted,
because delivered and read repeat the same pricing for the same message.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Channel, ChannelConnection, ConnectionStatus, WhatsAppUsage
from shared.utils.logging import get_logger

logger = get_logger(__name__)

CHARGED = "regular"
# "Business eligibility payment issue": no payment method on the WhatsApp account, a credit line over its limit, ...
PAYMENT_ERROR_CODES = {131042}
FREE_SERVICE_PER_MONTH = 1000


def month_start(at: datetime) -> datetime:
    at = at.astimezone(timezone.utc) if at.tzinfo else at
    return datetime(at.year, at.month, 1)


async def _connection(db: AsyncSession, phone_number_id: str | None) -> ChannelConnection | None:
    if not phone_number_id:
        return None
    return (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.channel == Channel.whatsapp,
                ChannelConnection.external_account_id == phone_number_id,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()


def payment_error(errors: list) -> dict | None:
    for err in errors or []:
        try:
            code = int(err.get("code"))
        except (TypeError, ValueError, AttributeError):
            continue
        if code in PAYMENT_ERROR_CODES:
            return {"code": code, "title": str(err.get("title") or err.get("message") or "")[:200]}
    return None


async def record(db: AsyncSession, update) -> None:
    """Count a sent message, or remember a payment failure. Never raises - a status must never break the webhook."""
    try:
        problem = payment_error(update.errors) if update.status == "failed" else None
        pricing = (update.raw or {}).get("pricing") or {}
        counts = update.status == "sent" and pricing.get("category")
        if not problem and not counts:
            return
        connection = await _connection(db, update.phone_number_id)
        if connection is None:
            return
        if problem:
            extra = dict(connection.extra or {})
            extra["payment_issue"] = {**problem, "at": datetime.now(timezone.utc).isoformat()}
            connection.extra = extra
            logger.warning("whatsapp payment issue business=%s code=%s", connection.business_id, problem["code"])
            return
        pricing_type = str(pricing.get("type") or ("regular" if pricing.get("billable") else "free"))[:40]
        stmt = insert(WhatsAppUsage).values(
            id=uuid.uuid4(), business_id=connection.business_id, month=month_start(update.occurred_at or datetime.now(timezone.utc)),
            category=str(pricing["category"])[:40], pricing_type=pricing_type, messages=1,
        ).on_conflict_do_update(
            constraint="uq_whatsapp_usage_month", set_={"messages": WhatsAppUsage.messages + 1},
        )
        await db.execute(stmt)
        if pricing_type == CHARGED and (connection.extra or {}).get("payment_issue"):
            # a charged message went out, so Meta has a way to bill again
            extra = dict(connection.extra)
            extra.pop("payment_issue", None)
            connection.extra = extra
    except Exception:  # noqa: BLE001
        logger.exception("whatsapp usage not recorded for status %s", getattr(update, "external_id", "?"))


async def this_month(db: AsyncSession, business_id: uuid.UUID, now: datetime | None = None) -> dict:
    """This month's messages by category: how many were charged and how many were free, plus any payment problem."""
    month = month_start(now or datetime.now(timezone.utc))
    rows = (
        await db.execute(
            select(WhatsAppUsage.category, WhatsAppUsage.pricing_type, WhatsAppUsage.messages).where(
                WhatsAppUsage.business_id == business_id, WhatsAppUsage.month == month,
            )
        )
    ).all()
    by_category: dict[str, dict[str, int]] = {}
    for category, pricing_type, messages in rows:
        slot = by_category.setdefault(category, {"charged": 0, "free": 0})
        slot["charged" if pricing_type == CHARGED else "free"] += messages
    connection = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == business_id,
                ChannelConnection.channel == Channel.whatsapp,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()
    service = by_category.get("service", {"charged": 0, "free": 0})
    return {
        "month": month.date().isoformat(),
        "by_category": by_category,
        "charged_total": sum(c["charged"] for c in by_category.values()),
        "service_sent": service["charged"] + service["free"],
        "free_service_per_month": FREE_SERVICE_PER_MONTH,
        "connected": connection is not None,
        "payment_issue": (connection.extra or {}).get("payment_issue") if connection else None,
    }
