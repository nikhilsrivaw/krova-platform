"""
An issue a business raised with KROVA's own team - from the Help page, often after KROVA Buddy could not answer.

The business sees its own tickets and the team's reply on the Help page; KROVA's operator works them in the admin
panel (GET/PATCH /admin/support-tickets). `transcript` is the Buddy conversation that led to the ticket, kept so the
operator does not have to ask the same questions again - the owner's own words about the product, never their
customers' messages.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class SupportTicketStatus(str, enum.Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"


class SupportTicketCategory(str, enum.Enum):
    bug = "bug"
    how_to = "how_to"
    billing = "billing"
    account = "account"
    feature_request = "feature_request"
    other = "other"


class SupportTicket(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "support_tickets"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[SupportTicketCategory] = mapped_column(
        EnumType(SupportTicketCategory, 20), nullable=False, default=SupportTicketCategory.other
    )
    details: Mapped[str] = mapped_column(Text, nullable=False)
    # The KROVA page it is about ("/automations"), when the owner said.
    page: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # [{role: "owner" | "ai", text}] - the Buddy chat before the ticket, if there was one.
    transcript: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    status: Mapped[SupportTicketStatus] = mapped_column(
        EnumType(SupportTicketStatus, 20), nullable=False, default=SupportTicketStatus.open
    )
    # KROVA team's answer, shown to the business on the Help page.
    reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replied_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("idx_support_tickets_business", "business_id", "created_at"),
        Index("idx_support_tickets_status", "status", "created_at"),
    )
