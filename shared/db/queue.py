"""
Job queue, on Postgres.

SELECT ... FOR UPDATE SKIP LOCKED is a correct queue: each worker claims rows
nobody else holds, without blocking, without a broker. At Krova's scale that
is a better trade than Redis - one less service to run, and, more usefully,
a job and the rows it touches commit or roll back together. A separate queue
can never promise that, which is how you end up with a message stored but its
follow-up job lost, or a job that runs against a transaction that rolled back.

Two failure modes are handled explicitly because both are quiet:

  A worker dies holding jobs. Those rows stay 'running' forever unless
  something reclaims them, so reclaim_stalled() exists and must be scheduled.

  A job fails repeatedly. Retries back off, and after max_attempts it stops
  and stays visible as failed rather than looping.
"""

import socket
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Job, JobStatus
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# How long a claimed job may run before we assume its worker died.
# Longer than the slowest job; short enough that a crash is not a long outage.
STALL_AFTER = timedelta(minutes=15)

WORKER_ID = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _backoff(attempts: int) -> timedelta:
    """Exponential, capped at an hour. 1m, 2m, 4m, 8m, 16m, ..."""
    return timedelta(seconds=min(60 * (2 ** max(0, attempts - 1)), 3600))


async def enqueue(
    queue: str,
    payload: dict,
    db: AsyncSession,
    *,
    run_after: datetime | None = None,
    max_attempts: int = 5,
) -> Job:
    """
    Add a job.

    Does not commit - the caller's transaction owns it. That is the point:
    the job becomes visible exactly when the work that justified it does.
    """
    job = Job(
        queue=queue,
        payload=payload,
        status=JobStatus.pending,
        run_after=run_after or _now(),
        max_attempts=max_attempts,
        created_at=_now(),
    )
    db.add(job)
    return job


async def claim(queue: str, db: AsyncSession, *, limit: int = 1) -> list[Job]:
    """
    Take up to `limit` jobs off a queue.

    SKIP LOCKED is what makes this safe to run from many workers at once:
    rows another worker has locked are stepped over rather than waited on, so
    workers never queue behind each other.
    """
    rows = await db.execute(
        text(
            """
            SELECT id FROM jobs
            WHERE queue = :queue
              AND status = 'pending'
              AND run_after <= :now
            ORDER BY run_after
            FOR UPDATE SKIP LOCKED
            LIMIT :limit
            """
        ),
        {"queue": queue, "now": _now(), "limit": limit},
    )
    job_ids = [r[0] for r in rows.fetchall()]
    if not job_ids:
        return []

    now = _now()
    await db.execute(
        update(Job)
        .where(Job.id.in_(job_ids))
        .values(
            status=JobStatus.running,
            locked_by=WORKER_ID,
            locked_at=now,
            attempts=Job.attempts + 1,
        )
    )
    result = await db.execute(select(Job).where(Job.id.in_(job_ids)))
    return list(result.scalars().all())


async def complete(job: Job, db: AsyncSession) -> None:
    job.status = JobStatus.done
    job.completed_at = _now()
    job.locked_by = None
    job.locked_at = None
    job.last_error = None


async def fail(job: Job, error: str, db: AsyncSession) -> None:
    """
    Record a failure and decide whether to retry.

    A job that has exhausted its attempts stays as 'failed' rather than being
    deleted. A queue that quietly discards what it could not do is a queue
    that loses a customer's message without telling anyone.
    """
    job.last_error = error[:2000]
    job.locked_by = None
    job.locked_at = None

    if job.attempts >= job.max_attempts:
        job.status = JobStatus.failed
        job.completed_at = _now()
        logger.error(
            "job %s on %s failed permanently after %s attempts: %s",
            job.id,
            job.queue,
            job.attempts,
            error[:300],
        )
    else:
        job.status = JobStatus.pending
        job.run_after = _now() + _backoff(job.attempts)
        logger.warning(
            "job %s on %s failed (attempt %s/%s), retrying at %s: %s",
            job.id,
            job.queue,
            job.attempts,
            job.max_attempts,
            job.run_after.isoformat(),
            error[:200],
        )


async def reclaim_stalled(db: AsyncSession) -> int:
    """
    Return jobs whose worker died to the pending pool.

    Must be scheduled. Without it, a single crash silently strands whatever
    that worker was holding, and nothing ever says so.
    """
    cutoff = _now() - STALL_AFTER
    result = await db.execute(
        update(Job)
        .where(Job.status == JobStatus.running, Job.locked_at < cutoff)
        .values(
            status=JobStatus.pending, locked_by=None, locked_at=None, run_after=_now()
        )
        .returning(Job.id)
    )
    reclaimed = len(result.fetchall())
    if reclaimed:
        logger.warning("reclaimed %s stalled job(s)", reclaimed)
    return reclaimed


async def queue_depth(db: AsyncSession) -> dict[str, dict[str, int]]:
    """Pending and failed counts per queue, for health checks and alerting."""
    result = await db.execute(
        text(
            """
            SELECT queue, status, count(*)
            FROM jobs
            WHERE status IN ('pending', 'running', 'failed')
            GROUP BY queue, status
            """
        )
    )
    depth: dict[str, dict[str, int]] = {}
    for queue, status, count in result.fetchall():
        depth.setdefault(queue, {})[status] = count
    return depth
