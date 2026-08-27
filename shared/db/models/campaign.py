"""
Campaigns.

Every competitor sells "broadcast": upload a list, pick a template, blast it.
The Krova version starts somewhere else entirely - the audience is a question
about the ledger, not a spreadsheet.

    "everyone who owes me money"
    "everyone whose payment is overdue"
    "everyone I promised something to and haven't delivered"
    "everyone who hasn't heard from me in 30 days"

That difference matters commercially as well as usefully. A blast to a
purchased list is marketing-category traffic, which Meta always charges for
and which drags a number's quality rating down. A payment reminder to someone
who genuinely owes you is utility-category - free inside the service window,
cheap outside it - and nobody marks it as spam because they were expecting it.

The same feature on a pricing page. A different thing underneath.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class Audience(str, enum.Enum):
    """
    Who to reach, expressed as a question rather than a list.

    Each of these is a query the business could not run anywhere else, because
    nobody else knows what was promised in their conversations.
    """

    owes_money = "owes_money"              # open they_owe commitments
    overdue = "overdue"                    # ...and past due
    we_promised = "we_promised"            # open we_owe - what the business owes
    gone_quiet = "gone_quiet"              # no contact in N days
    by_tag = "by_tag"                      # a CRM tag, confirmed or suggested-and-confirmed
    all_customers = "all_customers"        # the blunt instrument, still available


class CampaignStatus(str, enum.Enum):
    draft = "draft"
    scheduled = "scheduled"
    sending = "sending"
    sent = "sent"
    paused = "paused"        # tier limit reached; resumes tomorrow
    cancelled = "cancelled"
    failed = "failed"


class Campaign(UUIDMixin, TimestampMixin, Base):
    """One send to a segment."""

    __tablename__ = "campaigns"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    audience: Mapped[Audience] = mapped_column(EnumType(Audience, 24), nullable=False)
    # Parameters for the audience question - the day count for gone_quiet, a
    # minimum amount for owes_money.
    audience_params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Templates are how you reach someone outside the 24-hour window, which is
    # almost everyone in a campaign. A campaign without one can only reach
    # people who happen to have written recently.
    template_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    template_language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    # Which commitment fields fill the template's {{placeholders}}, in order.
    # e.g. ["customer_name", "amount", "due_date"] - resolved per recipient, so
    # each person sees their own figures.
    variable_mapping: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    status: Mapped[CampaignStatus] = mapped_column(
        EnumType(CampaignStatus, 20), nullable=False, default=CampaignStatus.draft
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Counts, kept as columns rather than derived - a campaign report should
    # not require scanning every message it sent.
    recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # What Meta charged us for, by category. Utility and marketing cost very
    # different amounts, and an owner should be able to see which they used.
    category: Mapped[str | None] = mapped_column(String(20), nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("idx_campaigns_business", "business_id", "status", "created_at"),
    )


class CampaignRecipient(UUIDMixin, Base):
    """
    One person in one campaign.

    A row per recipient rather than a count, so a business can answer "did
    Priya get it?" - which is the question they actually ask when someone
    says they never heard from you.
    """

    __tablename__ = "campaign_recipients"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )

    # pending | sent | failed | skipped
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # Why we did not send - "customer marked private", "no phone number",
    # "daily limit reached". Worth keeping: an owner who sees 40 sent out of
    # 60 wants to know about the other 20.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The values that filled the template for this person, so the sent message
    # can be reconstructed exactly.
    variables: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("idx_campaign_recipients", "campaign_id", "status"),
    )
