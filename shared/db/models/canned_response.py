"""
Saved replies: text a staff member has already written once and wants to
reuse verbatim - a refund policy, an address, an apology for a delay.

Deliberately separate from MessageDraft. A draft is the AI's own words,
held for a human to approve before anything is claimed on the business's
behalf; a canned response is a human's own words, written once and meant to
go out exactly as saved every time - the two should never be confused for
each other, so they get their own table rather than a "kind" flag bolted
onto drafts.
"""

import uuid

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin


class CannedResponse(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "canned_responses"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("idx_canned_responses_business", "business_id"),
    )
