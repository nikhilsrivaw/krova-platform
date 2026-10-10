import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, UUIDMixin


class ActivityLog(UUIDMixin, Base):
    """
    One thing a signed-in person did (or tried to do) in a business.

    The answer to "which team member did what": written for every state-
    changing request by anyone on the team, by the API itself rather than by
    each handler remembering to - a handler added next month is logged
    without anyone thinking about it (shared/audit/activity.py).

    Append-only: nothing in the app updates or deletes a row. A refused
    attempt is recorded too (outcome "denied"), because an agent trying to
    download the customer list is exactly what an owner would want to know.

    What is NOT stored: message text, customer details, file contents. A row
    says who did which action, to which record (ids), when, from where, and
    what it came to (counts) - enough to ask the person, not a second copy of
    the business's data.

    `user_label` is the person's name or email as it was at the time, so the
    row still reads sensibly after they are removed from the team (user_id is
    then set null, never the row deleted).
    """

    __tablename__ = "activity_log"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    user_label: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    role: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Stable key ("message_sent", "campaign_sent", "login") - what filters and
    # per-person counts group on - and a readable line for the feed.
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    summary: Mapped[str] = mapped_column(String(300), nullable=False)
    # ok | denied | failed
    outcome: Mapped[str] = mapped_column(String(10), nullable=False, default="ok")

    method: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    path: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Ids from the URL and counts the handler chose to note. Never bodies.
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_activity_business_time", "business_id", "occurred_at"),
        Index("idx_activity_business_user_time", "business_id", "user_id", "occurred_at"),
        Index("idx_activity_business_action_time", "business_id", "action", "occurred_at"),
    )
