"""
Message templates.

The capability that makes Krova a Tech Provider rather than a messaging
integration. Meta names it directly: a Tech Provider must "build systems
allowing clients to create message templates within your application."

It matters far beyond the checklist. Outside the 24-hour customer service
window, only an approved template will deliver - a free-form send is refused
with error 131047. So without templates a business can answer people who
wrote first, and nothing else: no payment reminder, no appointment
confirmation, no follow-up. That is most of what a business actually wants
to send.

Templates live in Meta's system, not ours. This table is a local mirror kept
in step by the message_template_status_update webhook, because approval is
asynchronous - a client submits a template and hears back up to 24 hours
later, long after the request that created it has finished.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class TemplateCategory(str, enum.Enum):
    """
    Meta's three categories, and the difference is money.

    Utility and authentication are free inside the customer service window and
    cheap outside it. Marketing is always charged. An agent that reaches for
    utility when utility will do is the difference between a platform that
    saves a business money and one that quietly spends it.
    """

    utility = "UTILITY"
    marketing = "MARKETING"
    authentication = "AUTHENTICATION"


class TemplateStatus(str, enum.Enum):
    """
    Where a template stands with Meta.

    `local` is ours, not Meta's: a template the client has written but not yet
    submitted. Everything else mirrors what Meta reports.
    """

    local = "LOCAL"
    pending = "PENDING"
    approved = "APPROVED"
    rejected = "REJECTED"
    paused = "PAUSED"
    disabled = "DISABLED"
    flagged = "FLAGGED"
    archived = "ARCHIVED"
    deleted = "DELETED"


class MessageTemplate(UUIDMixin, TimestampMixin, Base):
    """
    One template on one business's WhatsApp Business Account.

    Uniqueness is (business, name, language) because Meta treats a template
    name as a family with one entry per language - "payment_reminder" in en
    and hi are two templates sharing a name, and deleting by name removes
    both.
    """

    __tablename__ = "message_templates"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("channel_connections.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Meta's own id, once submitted. Null while the template is only local.
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Lowercase letters, digits and underscores only - Meta rejects anything else.
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")

    category: Mapped[TemplateCategory] = mapped_column(
        EnumType(TemplateCategory, 20), nullable=False
    )
    status: Mapped[TemplateStatus] = mapped_column(
        EnumType(TemplateStatus, 20), nullable=False, default=TemplateStatus.local
    )

    # The components array exactly as sent to Meta: HEADER, BODY, FOOTER,
    # BUTTONS. Stored whole rather than split into columns because Meta's
    # shape changes and an edit replaces every component at once - there is no
    # partial update to model.
    components: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Body text lifted out for search and display. Derived, never authoritative.
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Why Meta refused it, in Meta's words, so the client can act on it.
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Approved templates are capped at 10 edits per 30 days, or 1 per 24 hours.
    # Tracked here so a client is warned before Meta refuses them.
    edit_count: Mapped[int] = mapped_column(nullable=False, default=0)
    last_edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "business_id", "name", "language", name="uq_template_name_language"
        ),
        Index("idx_templates_business_status", "business_id", "status"),
        # The webhook arrives knowing only Meta's id, so this is the lookup
        # that has to be fast.
        Index("idx_templates_external", "external_id"),
    )

    @property
    def sendable(self) -> bool:
        """Only an approved template will actually deliver."""
        return self.status == TemplateStatus.approved
