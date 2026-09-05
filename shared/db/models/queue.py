"""
The OPD Queue capability: who is physically waiting right now, and where
they stand.

Deliberately not the Scheduling capability - an Appointment is a future
booked slot; a QueueEntry is a live physical-presence state ("checked in,
waiting, being seen") that a walk-in gets even with no Appointment row at
all. The two are related (a checked-in patient may also have an
Appointment), never merged: Scheduling answers "when is my slot", this
answers "how many people are ahead of me right now".
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import ARRAY, Date, DateTime, ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.models.scheduling import IntakeChannel
from shared.db.models.shift import Shift
from shared.db.types import EnumType


class QueueStatus(str, enum.Enum):
    waiting = "waiting"
    in_consultation = "in_consultation"
    done = "done"
    skipped = "skipped"
    cancelled = "cancelled"


class QueueEntry(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "queue_entries"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    # Nullable: a walk-in checks in before identity is always resolved.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    doctor_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("doctors.id", ondelete="SET NULL"), nullable=True
    )

    # Which shift this token belongs to - Emergency is a fully separate
    # sequence from Morning/Evening, see shared/db/models/shift.py. A token
    # can only be issued into a shift with a currently-open ShiftSession;
    # that check happens where the row is created, not here.
    shift: Mapped[Shift] = mapped_column(EnumType(Shift, 20), nullable=False)

    # Where the booking came from - same enum Appointment already uses, same
    # "never hidden from staff" reasoning.
    intake_channel: Mapped[IntakeChannel] = mapped_column(
        EnumType(IntakeChannel, 20), nullable=False
    )
    # Same provenance rule as Commitment/Insight/Appointment: an agent-issued
    # token must cite the conversation that created it. Null only for
    # intake_channel=manual (kiosk or staff check-in), where there is no
    # conversation to cite.
    source_message_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(PgUUID(as_uuid=True)), nullable=True
    )

    # The calendar date this ticket belongs to, in the business's own local
    # day - queue_number resets each shift each day, so "#4" means nothing
    # without both.
    queue_date: Mapped[datetime] = mapped_column(Date, nullable=False)
    queue_number: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[QueueStatus] = mapped_column(
        EnumType(QueueStatus, 20), nullable=False, default=QueueStatus.waiting
    )

    checked_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    called_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The live-list query: today's queue for a doctor, in order.
        Index("idx_queue_business_date_status", "business_id", "queue_date", "status"),
        Index("idx_queue_doctor_date", "doctor_id", "queue_date"),
        # Enforced at the DB level, not just application-side max+1 - a
        # kiosk check-in and an agent booking can race each other.
        UniqueConstraint(
            "business_id", "queue_date", "shift", "queue_number",
            name="uq_queue_number_per_shift_per_day",
        ),
    )
