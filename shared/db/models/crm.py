"""
The Krova CRM.

Every other CRM has the same failure mode: a tag field, a pipeline stage,
a notes box - all of it typed in by hand, all of it stale within a month,
because nobody has time to keep a second system honest about what is
happening in the first.

This one starts from what the platform already knows. A tag here is either
proposed by reading the same signals that already feed the ledger - a
missed commitment, a churn-risk mention, a health score the nightly
compression already computed - or it is a human's own words about
something no conversation could tell you. The first kind is never applied
silently: it sits as `suggested`, with its reasoning attached, until a
human says yes or no. Exactly the discipline `Commitment.status.unconfirmed`
already uses, because it is the same problem: never present a guess as a
fact.

A rejected suggestion is kept, not deleted - that is what stops the same
guess reappearing every night the rule that produced it fires again.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class TagStatus(str, enum.Enum):
    suggested = "suggested"  # proposed by the rule engine, awaiting a human
    confirmed = "confirmed"  # a human agreed, or a human wrote it themselves
    rejected = "rejected"    # a human said no - the label is never re-proposed


class CustomerTag(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "customer_tags"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )

    label: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[TagStatus] = mapped_column(
        EnumType(TagStatus, 20), nullable=False, default=TagStatus.confirmed
    )

    # One sentence on why the rule engine proposed this - the same discipline
    # as a commitment's source_quote. Null for a tag a human typed themselves,
    # since there is nothing to cite.
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # One row per (customer, label) ever - a rejected suggestion occupies
        # its slot so the rule engine's "already decided" check is one lookup.
        UniqueConstraint("customer_id", "label", name="uq_customer_tag_label"),
        Index("idx_customer_tags_business", "business_id", "status"),
        Index("idx_customer_tags_customer", "customer_id"),
    )


class CustomerNote(UUIDMixin, TimestampMixin, Base):
    """
    What no conversation could tell you.

    Deliberately the one manual part of this CRM. A note is never inferred
    and never suggested - it exists because a human decided it was worth
    writing down.
    """

    __tablename__ = "customer_notes"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("idx_customer_notes_customer", "customer_id"),)
