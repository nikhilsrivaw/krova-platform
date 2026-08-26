"""
Background work, queued in Postgres.

There is no Redis here. Postgres with SELECT ... FOR UPDATE SKIP LOCKED is a
correct job queue, and at this scale it is a better trade: one less managed
service, one less thing to run in a VPC, one less place for state to disagree
with the database. It also means a job and the rows it touches commit or roll
back together, which a separate queue can never promise.

Revisit if a single queue ever needs more than a few thousand jobs a second.
That is a long way from twenty clients.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, UUIDMixin
from shared.db.types import EnumType


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


class Job(UUIDMixin, Base):
    """
    One unit of background work.

    run_after carries both scheduling and retry backoff: a failed job is
    rescheduled by pushing this forward, so there is one mechanism rather
    than two.
    """

    __tablename__ = "jobs"

    # ingest_message | analyse_business | refresh_tokens | send_message ...
    queue: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    status: Mapped[JobStatus] = mapped_column(
        EnumType(JobStatus, 20), nullable=False, default=JobStatus.pending
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Which worker holds it, so a crashed worker's jobs can be reclaimed
    # rather than lost.
    locked_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # The claim query: oldest ready job on this queue.
        Index("idx_jobs_claim", "queue", "status", "run_after"),
        Index("idx_jobs_stuck", "status", "locked_at"),
    )
