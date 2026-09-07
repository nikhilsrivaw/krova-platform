"""
Delivers one outbound webhook per job - the Zapier-shaped "send booking
events wherever a business already points a catch-hook" integration.

Same claim-handle-commit shape as every other worker (see
shared/db/worker_runner.py). A slow or dead endpoint on a business's own
side must never affect Krova's own request path - that's the whole reason
this is a queued job and not an inline httpx call at booking time.
"""

import json
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db import queue
from shared.db.models import Job, OutboundWebhook
from shared.db.worker_runner import run_worker_process
from shared.integrations import webhooks
from shared.utils.logging import get_logger

logger = get_logger(__name__)

QUEUE = webhooks.QUEUE

_TIMEOUT = 15.0


def _slack_line(event_type: str | None, data: dict) -> str:
    """
    One readable line for a Slack/Teams incoming webhook - both platforms
    accept the same {"text": "..."} shape, so one formatter covers both
    rather than two near-identical ones.
    """
    if event_type == "escalation.raised":
        return f":rotating_light: Krova escalated on *{data.get('channel', 'unknown')}*: {data.get('reason', 'needs review')}"
    if event_type == "appointment.booked":
        return f":calendar: New appointment booked (id `{data.get('appointment_id', '?')}`)"
    if event_type == "appointment.cancelled":
        return f":x: Appointment cancelled (id `{data.get('appointment_id', '?')}`)"
    if event_type == "queue_token.issued":
        return f":ticket: Queue token #{data.get('queue_number', '?')} issued ({data.get('shift', '?')} shift)"
    if event_type == "competitor.mentioned":
        quote = data.get("source_quote") or data.get("title", "")
        return f":dart: Competitor mentioned ({data.get('severity', 'info')}): \"{quote}\""
    return f"Krova event: {event_type}"


def _serialize(webhook: OutboundWebhook, event_type: str | None, data: dict) -> bytes:
    if webhook.format in ("slack", "teams"):
        return json.dumps({"text": _slack_line(event_type, data)}).encode()
    # "raw" (default) - today's exact shape, unchanged.
    return json.dumps({"event_type": event_type, "data": data}).encode()


async def _run_job(job: Job, db: AsyncSession) -> None:
    payload = job.payload or {}
    webhook_id = payload.get("webhook_id")
    if not webhook_id:
        await queue.fail(job, "job payload has no webhook_id", db)
        return

    webhook = await db.get(OutboundWebhook, uuid.UUID(webhook_id))
    if webhook is None or not webhook.active:
        # Deleted or disabled since this was queued - nothing to deliver.
        await queue.complete(job, db)
        return

    body = _serialize(webhook, payload.get("event_type"), payload.get("payload") or {})
    signature = webhooks.sign(webhook.secret, body)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                webhook.target_url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Krova-Signature-256": f"sha256={signature}",
                },
            )
    except httpx.HTTPError as exc:
        webhook.failure_count += 1
        webhook.last_delivery_status = f"error: {type(exc).__name__}"
        await queue.fail(job, f"{type(exc).__name__}: {exc}", db)
        return

    webhook.last_delivery_at = datetime.now(timezone.utc)

    if 200 <= response.status_code < 300:
        webhook.failure_count = 0
        webhook.last_delivery_status = "ok"
        await queue.complete(job, db)
    else:
        webhook.failure_count += 1
        webhook.last_delivery_status = f"http {response.status_code}"
        await queue.fail(job, f"target returned {response.status_code}", db)


if __name__ == "__main__":
    run_worker_process(QUEUE, _run_job, worker_name="webhook_delivery")
