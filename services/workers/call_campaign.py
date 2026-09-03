"""
Dials one outbound campaign call per job.

Same shape as every other worker here (see shared/db/worker_runner.py) -
claim, handle, commit, repeat. Deliberately not sync-inline the way
campaigns.py's WhatsApp send_campaign is: a phone call can take a minute
or more, and holding one HTTP request open per recipient (or looping
inline through a whole campaign) is a completely different latency shape
from a WhatsApp API round-trip - this queue exists specifically so that
mismatch is never repeated for voice.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from shared.channels.voice import outbound
from shared.db import queue
from shared.db.models import Job
from shared.db.worker_runner import run_worker_process
from shared.utils.logging import get_logger

logger = get_logger(__name__)

QUEUE = "call_campaign_dial"


async def _run_job(job: Job, db: AsyncSession) -> None:
    raw_id = (job.payload or {}).get("recipient_id")
    if not raw_id:
        await queue.fail(job, "job payload has no recipient_id", db)
        return
    try:
        await outbound.place_call(uuid.UUID(raw_id), db)
        await queue.complete(job, db)
    except Exception as exc:  # noqa: BLE001 - the queue decides what to do next
        await queue.fail(job, f"{type(exc).__name__}: {exc}", db)


if __name__ == "__main__":
    run_worker_process(QUEUE, _run_job, worker_name="call_campaign")
