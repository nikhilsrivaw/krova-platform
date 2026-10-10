import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base


class ThreadPresence(Base):
    """
    Who has a conversation open right now, and who is typing in it.

    One row per (customer, person), refreshed by the open thread every few
    seconds; a row older than shared/team/presence.py's window means the person
    left. It is what lets a teammate see "Rahul is replying" before sending a
    second answer to the same customer. Nothing here is history - rows are
    overwritten, and nobody reads them back later.
    """

    __tablename__ = "thread_presence"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    typing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("idx_thread_presence_seen", "customer_id", "seen_at"),)
