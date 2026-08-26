"""
The Case Tracking capability: a client's matter, its status, and the clock
running on it.

Deliberately not the Scheduling capability reused with different labels - a
court sets a hearing date, a client does not book one through self-service
chat the way a patient books a doctor. What a case needs is a record of
where the matter stands and what date is coming, surfaced to the lawyer and
answered honestly (never predicted) to the client. Hearing dates and
deadlines are still tracked as Commitment(kind="meeting") rows via the same
extraction pipeline every vertical already gets - this table is the
structured matter record extraction has nothing to hang off, not a
replacement for it.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class CaseStatus(str, enum.Enum):
    intake = "intake"        # client has approached, matter not yet opened
    active = "active"
    on_hold = "on_hold"
    closed = "closed"


class Case(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "cases"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    assigned_to_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Often unassigned at intake - a matter can exist before the court or
    # firm has issued a number for it.
    case_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    opposing_party: Mapped[str | None] = mapped_column(String(255), nullable=True)
    court: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[CaseStatus] = mapped_column(
        EnumType(CaseStatus, 20), nullable=False, default=CaseStatus.intake
    )
    # Denormalised onto the case for cheap "what's coming up" queries and
    # context rendering - the authoritative record of the promise itself
    # still lives in Commitment, cited back to the message that gave it.
    next_hearing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_cases_business", "business_id"),
        Index("idx_cases_customer", "customer_id"),
        Index("idx_cases_business_hearing", "business_id", "next_hearing_at"),
    )
