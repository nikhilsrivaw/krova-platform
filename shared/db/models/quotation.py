"""
Quotations: the thing B2B actually runs on, and the thing nobody tracks.

Research (docs/new/type-1-research.md) found the same gap stated two ways:
*"once a quotation is sent, most small businesses have no system to track
its status or schedule follow-ups - pending quotes pile up in email inboxes
and WhatsApp chats"*, and deals still sitting in the proposal stage past 21
days have a **70% lower win rate**. The quote is sent in a conversation
Krova already reads; what was missing was anywhere to put it.

**Why this is its own model rather than a Commitment.** A quotation was
considered as another `CommitmentKind`, which would have inherited the
sweeps and the ledger for free. It is not one. A commitment is a promise
already made, with one amount and one deadline; a quotation is an *offer*
that gets revised, carries line items, expires on its own terms, and ends
won or lost rather than met or missed. Stretching Commitment to cover it
would have made both concepts vaguer. Built clean, deliberately, at the
cost of re-implementing the follow-up sweep.

Line items are a real table rather than JSONB (which is how `Order.items`
does it) so they can join to `ProductVariant`. That join is the point: it
is what makes "we quote this SKU constantly and never win it" a question
that can be asked at all.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class QuotationStatus(str, enum.Enum):
    """
    Where an offer stands.

    `sent` and `negotiating` are both open states, kept apart because they
    need different follow-up: silence after sending is the 21-day problem,
    while an active negotiation is already warm and should not be chased
    the same way.

    `expired` is reached by `valid_until` passing, not by a human deciding
    it - an offer nobody answered is a real outcome worth counting, and
    folding it into `lost` would hide how much revenue simply goes cold.
    """

    draft = "draft"
    sent = "sent"
    negotiating = "negotiating"
    won = "won"
    lost = "lost"
    expired = "expired"
    withdrawn = "withdrawn"


# The states where chasing still makes sense. Every sweep and dashboard
# query means this by "open", so it lives here rather than being re-listed
# at each call site.
OPEN_STATUSES = (QuotationStatus.draft, QuotationStatus.sent, QuotationStatus.negotiating)


class Quotation(UUIDMixin, TimestampMixin, Base):
    """One offer made to one buyer."""

    __tablename__ = "quotations"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )

    # The business's own quote number, as they say it to the customer
    # ("QT-2026-118"). Theirs, not ours - a reference the buyer will quote
    # back is worth more than an id only we understand.
    reference: Mapped[str | None] = mapped_column(String(80), nullable=True)

    status: Mapped[QuotationStatus] = mapped_column(
        EnumType(QuotationStatus, 20), nullable=False, default=QuotationStatus.draft
    )

    # Null when the quote was discussed without a firm number - common in
    # chat, and better than inventing a total nobody said.
    total_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")

    # What the offer says about itself. `valid_until` drives expiry;
    # `sent_at` is what the 21-day staleness clock runs from, and the two
    # are different dates surprisingly often.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Follow-up state. Separate columns rather than a reused reminder stamp,
    # for the reason Commitment's own docstrings give about shared stamps
    # letting one send silently block another.
    last_followed_up_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    follow_up_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Outcome, once known.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Free text on purpose: the real reasons ("competitor was cheaper",
    # "project postponed") do not fit a fixed list, and a wrong fixed list
    # produces worse data than no list.
    outcome_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Revisions. A revised quote is a new row pointing back at the one it
    # replaces, rather than an edit - what was originally offered is often
    # the most useful thing to look back at, and an edit destroys it.
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("quotations.id", ondelete="SET NULL"), nullable=True
    )

    # Provenance, same discipline as Commitment/Insight: when this was
    # extracted from a conversation rather than typed in, cite the messages
    # it came from. Empty for a manually created quote.
    source_message_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PgUUID(as_uuid=True)), nullable=False, default=list
    )
    # Verbatim wording the offer was read out of, kept so a person can
    # check the extraction rather than trust it.
    source_quote: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # A business's own reference should not repeat. Nullable, so quotes
        # captured from chat without a number are unaffected.
        UniqueConstraint("business_id", "reference", name="uq_quotation_reference"),
        # The sweep's query: this business's open quotes, oldest first.
        Index("idx_quotations_open", "business_id", "status", "sent_at"),
        Index("idx_quotations_customer", "customer_id", "status"),
        Index("idx_quotations_expiry", "status", "valid_until"),
    )


class QuotationItem(UUIDMixin, TimestampMixin, Base):
    """
    One line on a quotation.

    `variant_id` is nullable because a quote in chat often names something
    loosely ("the 12mm ones") before anyone has matched it to a catalogue
    row - and refusing to store the line until it resolves would mean
    storing nothing at all. `description` always holds what was actually
    said; the link is an enrichment on top, not a requirement.
    """

    __tablename__ = "quotation_items"

    quotation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    variant_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True
    )

    description: Mapped[str] = mapped_column(Text, nullable=False)
    # Quantity as a string: B2B quotes say "50 pcs", "2 cartons", "1 lot".
    # Forcing a number here would lose the unit, which is often the part
    # that matters in a dispute.
    quantity: Mapped[str | None] = mapped_column(String(80), nullable=True)
    unit_price_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_total_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)

    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("idx_quotation_items_quotation", "quotation_id", "position"),
        # The question this table exists for: which SKUs get quoted, and
        # which of those quotes are won.
        Index("idx_quotation_items_variant", "business_id", "variant_id"),
    )
