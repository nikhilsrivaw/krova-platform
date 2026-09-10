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

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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

    # Present only for a carousel template: one entry per card, in template
    # order - {"media_id": "...", "variable_mapping": [...]}. The media_id is
    # fixed (a business's own photo, uploaded once); the variable mapping
    # works exactly like variable_mapping above, just scoped to one card's
    # text instead of the message body.
    carousel_cards: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

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


class CampaignStep(UUIDMixin, TimestampMixin, Base):
    """
    One follow-up after a campaign's own first send.

    A campaign's own columns above (template_name, audience, ...) already
    are "step 0" - unchanged, unaffected if a campaign has no steps at all.
    A sequence is nothing more than several of these rows in order; there is
    no separate workflow concept, no canvas, no versioning to build - the
    same reasoning shared/verticals/__init__.py argues for templates over an
    empty builder applies here too.
    """

    __tablename__ = "campaign_steps"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)  # 1, 2, 3...

    # Days after THIS recipient's previous step actually sent, not after the
    # campaign started - someone who received step 1 late (daily-limit
    # spillover) still gets step 2 the right number of days after their own
    # step 1, not everyone else's.
    delay_days: Mapped[int] = mapped_column(Integer, nullable=False)

    # "always" fires regardless of what the recipient did; "no_reply" only
    # if they haven't written back since their previous step. A fixed
    # choice, not an authored condition language - the same discipline
    # Audience already applies to who a campaign reaches in the first
    # place: real, already-tracked signals only.
    condition: Mapped[str] = mapped_column(String(20), nullable=False, default="no_reply")
    # If the recipient replies at any point, they drop out of every
    # remaining step in this sequence, even a later "always" one - a
    # business does not want to keep pestering someone who already engaged.
    stop_on_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    template_name: Mapped[str] = mapped_column(String(512), nullable=False)
    template_language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    variable_mapping: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    carousel_cards: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (
        UniqueConstraint("campaign_id", "step_order", name="uq_campaign_step_order"),
        Index("idx_campaign_steps_campaign", "campaign_id"),
    )


class CampaignStepRecipient(UUIDMixin, Base):
    """
    One person's outcome for one follow-up step - the same shape as
    CampaignRecipient, one level deeper, for the identical reason: "did
    Priya get the reminder" needs a row, not just a count.
    """

    __tablename__ = "campaign_step_recipients"

    step_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("campaign_steps.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalised alongside step_id, same pattern CampaignRecipient itself
    # uses for campaign_id - lets the sweep and the UI query by campaign
    # without a join through campaign_steps first.
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )

    # pending | sent | failed | skipped
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # Why this step didn't send this person a message - "replied since the
    # previous step", "daily limit reached", "no phone number on file".
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    variables: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("step_id", "customer_id", name="uq_campaign_step_recipient"),
        Index("idx_campaign_step_recipients", "campaign_id", "status"),
    )
