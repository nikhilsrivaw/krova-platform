"""
The claim-process-backoff loop every queue worker runs, written once.

Every worker process (commitment extraction, drafting, profile compression)
was the same shape: claim a job from Postgres, hand it to a per-job handler,
commit, sleep when idle, back off on an unexpected error, finish the job in
hand rather than abandoning it mid-write on SIGTERM. That shape used to be
copy-pasted per worker - three real, independent processes maintaining the
same loop with the same chance to drift apart. This is the one copy; a
worker module now only writes what makes it different from the others: its
queue name and what one job actually does.
"""

import asyncio
import signal
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from shared.db import queue
from shared.db.models import Job
from shared.db.session import AsyncSessionLocal
from shared.utils.logging import get_logger

logger = get_logger(__name__)

JobHandler = Callable[[Job, AsyncSession], Awaitable[None]]

IDLE_SLEEP = 2.0
ERROR_BACKOFF = 5.0


async def run_worker(
    queue_name: str,
    handler: JobHandler,
    *,
    worker_name: str | None = None,
    idle_sleep: float = IDLE_SLEEP,
    error_backoff: float = ERROR_BACKOFF,
    stop: asyncio.Event | None = None,
) -> None:
    """
    Claim and process jobs from `queue_name` until told to stop.

    `handler` decides what one job means - it is responsible for calling
    `queue.complete`/`queue.fail` itself, since only it knows what "this job
    succeeded" means for its own payload shape. This loop only owns the
    claiming, the commit around it, and recovering from a handler that
    raised instead of failing the job cleanly.
    """
    stop = stop or asyncio.Event()
    name = worker_name or queue_name
    logger.info("%s worker started (%s)", name, queue.WORKER_ID)

    while not stop.is_set():
        try:
            async with AsyncSessionLocal() as db:
                await queue.reclaim_stalled(db)
                jobs = await queue.claim(queue_name, db, limit=1)
                await db.commit()

                if not jobs:
                    await _sleep_or_stop(idle_sleep, stop)
                    continue

                for job in jobs:
                    await handler(job, db)
                await db.commit()
        except Exception:
            logger.exception("%s worker loop error, backing off", name)
            await _sleep_or_stop(error_backoff, stop)

    logger.info("%s worker stopped", name)


async def _sleep_or_stop(seconds: float, stop: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


def run_worker_process(queue_name: str, handler: JobHandler, *, worker_name: str | None = None) -> None:
    """
    The `if __name__ == "__main__":` body every worker module used to
    duplicate: register SIGINT/SIGTERM to finish the job in hand rather than
    abandon it mid-write, then run the loop until stopped.
    """
    stop = asyncio.Event()

    def _handle_signal(*_: object) -> None:
        logger.info("shutdown requested, finishing current job")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, AttributeError):
            pass

    asyncio.run(run_worker(queue_name, handler, worker_name=worker_name, stop=stop))
