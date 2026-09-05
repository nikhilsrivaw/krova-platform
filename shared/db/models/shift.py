"""
Shift sessions - who's allowed to hand out a token right now.

An Indian clinic's OPD runs on shifts (Morning/Evening/Emergency), not
calendar slots - a patient gets "Morning #47", not "3:15pm with Dr. X". A
shift only exists for the day once staff opens it (the front desk opens
Morning when the doctor actually arrives); nothing - not the kiosk, not the
voice/WhatsApp agent, not a staff check-in - can hand out a token into a
shift with no open session. Emergency is deliberately its own shift with
its own token sequence, not a priority flag on the regular queue: severity
decides who's seen next in an emergency, not "next number called".
"""

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class Shift(str, enum.Enum):
    morning = "morning"
    evening = "evening"
    emergency = "emergency"


class ShiftSession(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "shift_sessions"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    shift: Mapped[Shift] = mapped_column(EnumType(Shift, 20), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    opened_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Null while open. Reopening the same shift the same day clears this
    # back to null rather than inserting a second row - see the unique
    # constraint below.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "business_id", "shift", "session_date", name="uq_shift_session_per_day"
        ),
        Index("idx_shift_sessions_business_date", "business_id", "session_date"),
    )
