"""
What Krova works out from conversations.

Every table here carries source_message_ids. Nothing derived exists without a
record of what it was derived from, and that single rule buys three things
that are otherwise hard.

Trust: an owner can click any number and see the three messages behind it.
For a product whose whole premise is telling people things about their money,
being able to show the working is the reason they will believe the number.

Honesty: an extraction that cites a message id we cannot find is rejected
rather than stored. That is a cheap and effective catch for an invented
commitment, and invented commitments are far more damaging than missed ones.

Deletion that is real: when a customer deletes a conversation, we can find
everything derived from it and remove that too. Without provenance you can
only hide the source, which is not erasure.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class CommitmentDirection(str, enum.Enum):
    # Promises run both ways, and both matter. What the business owes drives
    # reputation; what it is owed drives cash.
    we_owe = "we_owe"
    they_owe = "they_owe"


class CommitmentKind(str, enum.Enum):
    payment = "payment"
    delivery = "delivery"
    callback = "callback"
    document = "document"
    meeting = "meeting"
    other = "other"


class CommitmentStatus(str, enum.Enum):
    open = "open"
    met = "met"
    missed = "missed"
    cancelled = "cancelled"
    # Extracted but below the confidence threshold: shown to the owner for a
    # yes or no, never acted on until they answer.
    unconfirmed = "unconfirmed"


class Commitment(UUIDMixin, TimestampMixin, Base):
    """
    A promise found in a conversation.

    This is the bridge between talking and money. A commitment is a pre-invoice:
    it exists from the moment someone says "I'll pay by Friday", weeks before
    anything reaches accounting. That gap is the product.
    """

    __tablename__ = "commitments"

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

    direction: Mapped[CommitmentDirection] = mapped_column(EnumType(CommitmentDirection, 20), nullable=False)
    kind: Mapped[CommitmentKind] = mapped_column(
        EnumType(CommitmentKind, 20), nullable=False, default=CommitmentKind.other
    )

    # In the words the conversation used, so the owner recognises it.
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # Integer paise. Null when the promise has no amount ("I'll send the file").
    amount_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")

    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # True when the date was stated outright, false when we inferred it from
    # something like "next week". Affects how hard we are willing to chase.
    due_at_explicit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    status: Mapped[CommitmentStatus] = mapped_column(
        EnumType(CommitmentStatus, 20), nullable=False, default=CommitmentStatus.open
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    # The messages this was read from. Required - see the module docstring.
    source_message_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PgUUID(as_uuid=True)), nullable=False
    )
    # The words that carried the promise, for the owner to see at a glance.
    source_quote: Mapped[str | None] = mapped_column(Text, nullable=True)

    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        # The ledger's main query: what is open and overdue, soonest first.
        Index("idx_commitments_open", "business_id", "status", "due_at"),
        Index("idx_commitments_customer", "customer_id", "status"),
    )


class BusinessDNA(TimestampMixin, Base):
    """
    What the business is, in a form the agent can be told in every prompt.

    Seeded by the vertical template at signup so the agent is useful before a
    single conversation exists, then refined from what actually gets said.
    """

    __tablename__ = "business_dna"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        primary_key=True,
    )

    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    offerings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    pricing_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    opening_hours: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    policies: Mapped[str | None] = mapped_column(Text, nullable=True)
    tone: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Things the agent must never guess at. Every escalation adds to this, so
    # the product gets better by being honest about what it does not know.
    known_gaps: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # template | learned | edited - so a human edit is never overwritten by
    # the next nightly run.
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="template")


class CustomerIntelligence(TimestampMixin, Base):
    """
    The compressed profile the live agent reads.

    This is what makes sub-second voice possible on top of years of history.
    The agent never reads two hundred messages; it reads a few lines the
    overnight worker distilled from them. Compression is the cold path's real
    job, and this table is where it lands.
    """

    __tablename__ = "customer_intelligence"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )

    # A few lines, not a transcript. Goes into the prompt verbatim.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    health_score: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    open_commitments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outstanding_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    preferences: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Which channel they actually reply on, learned rather than declared.
    preferred_channel: Mapped[str | None] = mapped_column(String(20), nullable=True)

    source_message_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PgUUID(as_uuid=True)), nullable=False, default=list
    )
    computed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("idx_cust_intel_business", "business_id"),)


class Insight(UUIDMixin, Base):
    """
    Something worth telling the owner, with the evidence attached.

    Every competitor's AI says "you have 7 overdue payments" and the owner has
    to take it on faith. This one shows the messages.
    """

    __tablename__ = "insights"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=True
    )

    # overdue_payment | demand_signal | competitor_mention | churn_risk | ...
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="info")

    source_message_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PgUUID(as_uuid=True)), nullable=False, default=list
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    dismissed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("idx_insights_business_open", "business_id", "dismissed_at", "created_at"),
    )
