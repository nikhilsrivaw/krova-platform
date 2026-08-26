"""
The Scheduling capability: doctor calendars, bookable slots, appointments.

This is the "hospital vertical, done properly" piece - not just detecting
"we agreed to meet" in a conversation after the fact (that's what
Commitment(kind="meeting") already does), but a real source of truth for who
is free when, so voice and WhatsApp can both check it and write to it, and a
front desk sees one calendar no matter which channel the booking came from.

Availability is stored as recurring weekly rules (a doctor's Tuesday hours),
not materialized slot rows - a slot is a computed point in time, derived from
a rule plus what's already booked. Changing a doctor's hours is one row edit,
not a rewrite of a slot table.

This module is deliberately not vertical-specific in name or code - it is the
Scheduling *capability*, which Clinics and (eventually) Salons both declare in
their vertical template. See shared/verticals and the project's capability-
module convention: no per-vertical subclassing, ever.
"""

import enum
import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class AppointmentStatus(str, enum.Enum):
    requested = "requested"    # booked, not yet confirmed by staff (rare - most
                                # bookings self-confirm once the slot is held)
    confirmed = "confirmed"
    visited = "visited"
    no_show = "no_show"
    cancelled = "cancelled"


class IntakeChannel(str, enum.Enum):
    """Where the booking came from - shown to staff, never hidden."""

    voice = "voice"
    whatsapp = "whatsapp"
    manual = "manual"  # entered by staff directly, e.g. a walk-in


class Department(UUIDMixin, TimestampMixin, Base):
    """A routing bucket for doctors and inquiries. Optional - a single-doctor
    clinic has no real need for one, so doctors may leave this null."""

    __tablename__ = "departments"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    doctors: Mapped[list["Doctor"]] = relationship(back_populates="department")

    __table_args__ = (
        Index("idx_departments_business", "business_id"),
        UniqueConstraint("business_id", "name", name="uq_department_name_per_business"),
    )


class Doctor(UUIDMixin, TimestampMixin, Base):
    """
    A provider who can be booked.

    Fee and hours are what the voice/WhatsApp agent is allowed to quote -
    matches clinic.json's policy ("Quote only fees listed in the business
    details"): this table *is* those business details for this vertical,
    not a second, separately-maintained source that can drift from it.
    """

    __tablename__ = "doctors"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # e.g. "MBBS, MD (Cardiology)" - shown verbatim when the agent answers
    # "who is Dr. X" rather than invented from the name alone.
    qualifications: Mapped[str | None] = mapped_column(String(255), nullable=True)

    consultation_fee_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    department: Mapped["Department | None"] = relationship(back_populates="doctors")
    availability_rules: Mapped[list["AvailabilityRule"]] = relationship(
        back_populates="doctor", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("idx_doctors_business", "business_id"),)


class AvailabilityRule(UUIDMixin, TimestampMixin, Base):
    """
    One recurring block of a doctor's week - "Tuesdays, 10:00-13:00, 15-minute
    slots". A doctor's full week is however many rows this takes; a one-off
    change (leave, a holiday) is handled as an AvailabilityException, not by
    editing this rule and editing it back.
    """

    __tablename__ = "availability_rules"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("doctors.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Monday = 0 ... Sunday = 6, matching Python's date.weekday().
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    slot_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15)

    doctor: Mapped["Doctor"] = relationship(back_populates="availability_rules")

    __table_args__ = (
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="ck_availability_weekday"),
        CheckConstraint("end_time > start_time", name="ck_availability_time_order"),
        CheckConstraint("slot_duration_minutes > 0", name="ck_availability_slot_duration"),
        Index("idx_availability_doctor", "doctor_id"),
    )


class AvailabilityException(UUIDMixin, TimestampMixin, Base):
    """
    A doctor unavailable (or exceptionally available) on one specific date -
    leave, a conference, an extra Saturday clinic. Checked after the recurring
    rules, so it can block or add a whole day without touching the pattern.
    """

    __tablename__ = "availability_exceptions"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("doctors.id", ondelete="CASCADE"),
        nullable=False,
    )

    # A calendar date, not an instant - "Friday the 12th", not a specific
    # moment - so it's matched against the business's own local calendar,
    # never a UTC-shifted timestamp range.
    date: Mapped[date] = mapped_column(Date, nullable=False)
    is_unavailable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Only meaningful when is_unavailable is False (an added block of hours).
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("idx_availability_exceptions_doctor_date", "doctor_id", "date"),
    )


class Appointment(UUIDMixin, TimestampMixin, Base):
    """
    A booked slot. The single source of truth a front desk trusts, whichever
    channel it came from - intake_channel is never hidden from staff.

    source_message_ids follows the same provenance rule as Commitment and
    Insight: an AI-booked appointment must cite the conversation that booked
    it. Null only for intake_channel=manual, where there is no conversation
    to cite.
    """

    __tablename__ = "appointments"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("doctors.id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Only meaningful for a business with the property_listings capability -
    # which property a viewing is for. Nullable and SET NULL on delete: a
    # clinic appointment has none, and a withdrawn listing should not take
    # its viewing history down with it.
    property_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("properties.id", ondelete="SET NULL"),
        nullable=True,
    )

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[AppointmentStatus] = mapped_column(
        EnumType(AppointmentStatus, 20), nullable=False, default=AppointmentStatus.confirmed
    )
    intake_channel: Mapped[IntakeChannel] = mapped_column(
        EnumType(IntakeChannel, 20), nullable=False
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 24h / 2h reminder sends, tracked so the reminder worker never double-sends.
    reminder_24h_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reminder_2h_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    source_message_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(PgUUID(as_uuid=True)), nullable=True
    )

    __table_args__ = (
        Index("idx_appointments_business", "business_id"),
        Index("idx_appointments_customer", "customer_id"),
        # The reminder worker's main query: confirmed appointments coming up.
        Index("idx_appointments_doctor_time", "doctor_id", "starts_at"),
        # A property's viewing history - who has seen it, and when.
        Index("idx_appointments_property", "property_id"),
        # Slot-grid booking (see shared/scheduling) means two live appointments
        # can never legitimately share a start time - cheap, DB-level double-
        # booking protection without needing a range-exclusion constraint.
        # Cancelled appointments free the slot back up.
        Index(
            "uq_appointments_doctor_slot",
            "doctor_id",
            "starts_at",
            unique=True,
            postgresql_where=text("status != 'cancelled'"),
        ),
    )
