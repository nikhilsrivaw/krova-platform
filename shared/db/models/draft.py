"""
What the agent wants to say, before anyone lets it.

Krova's public promise is human-in-the-loop, so a drafted reply is a row here
rather than a message on its way out. Nothing reaches a customer until either
a person approves it, or the business has explicitly raised its autonomy to
`act` - and even then the draft is kept, so there is always a record of what
was said and why.

The `reasoning` and `used_context` columns exist for the same reason the
ledger cites its messages: an owner deciding whether to trust the agent needs
to see what it read and why it answered that way. A draft you cannot inspect
is a draft you cannot approve with any confidence.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class DraftStatus(str, enum.Enum):
    pending = "pending"      # waiting for a human
    approved = "approved"    # a person said yes; queued to send
    sent = "sent"
    rejected = "rejected"    # a person said no - the most useful signal we get
    expired = "expired"      # the 24-hour window closed before anyone looked
    superseded = "superseded"  # the customer wrote again; this reply is stale


class DraftAction(str, enum.Enum):
    """
    What the agent concluded it should do.

    `escalate` is not a failure. An agent that answers everything is worse
    than one that knows what it does not know - and every escalation names a
    gap the owner can close, which is how the product gets better by being
    honest about its limits.
    """

    reply = "reply"
    escalate = "escalate"
    no_action = "no_action"


class MessageDraft(UUIDMixin, TimestampMixin, Base):
    """A reply the agent proposes, and the reasoning behind it."""

    __tablename__ = "message_drafts"

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
    # The message this is answering. If the customer writes again before
    # anyone approves, the draft is superseded rather than sent late.
    in_reply_to_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )

    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[DraftAction] = mapped_column(
        EnumType(DraftAction, 20), nullable=False, default=DraftAction.reply
    )
    status: Mapped[DraftStatus] = mapped_column(
        EnumType(DraftStatus, 20), nullable=False, default=DraftStatus.pending
    )

    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Why it answered this way, in a sentence. Shown next to the draft.
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What it could not answer, when action is escalate. This is the column
    # that compounds: twenty of these show the owner exactly which gap to fill.
    gap: Mapped[str | None] = mapped_column(Text, nullable=True)

    confidence: Mapped[float] = mapped_column(nullable=False, default=0.0)

    # Which messages it read. Same discipline as the ledger - a draft that
    # cannot show its sources cannot be judged.
    used_context: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PgUUID(as_uuid=True)), nullable=False, default=list
    )

    # Outside the 24-hour window a free-form reply will not deliver, so a
    # draft has a shelf life and should not be offered after it lapses.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # What the person changed before sending. The difference between what the
    # agent wrote and what a human sent is the best training signal available.
    edited_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    sent_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )

    cost_paise: Mapped[int] = mapped_column(nullable=False, default=0)
    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        # The approvals queue: what is waiting, oldest first.
        Index("idx_drafts_pending", "business_id", "status", "created_at"),
        Index("idx_drafts_customer", "customer_id", "status"),
    )

    @property
    def final_body(self) -> str | None:
        """What actually gets sent - the human's edit wins over the agent's."""
        return self.edited_body or self.body
