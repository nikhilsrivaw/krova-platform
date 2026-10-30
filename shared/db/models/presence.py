import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, UUIDMixin


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


class ConversationRead(UUIDMixin, Base):
    """
    When one person last read one customer's chat - what "3 new" is counted from. Personal, unlike assignment: two
    teammates can each have their own unread count on the same chat. A reply from anyone also counts as read (see
    services/api/routers/conversations.py), so a chat someone has answered is never shown as unread.
    """

    __tablename__ = "conversation_reads"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    last_read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "customer_id", name="uq_conversation_read"),
        Index("idx_conversation_reads_user", "user_id", "business_id"),
    )
