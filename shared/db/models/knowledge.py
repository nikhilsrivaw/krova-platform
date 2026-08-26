"""
What a business knows, that the agent should too.

Competitors call this a knowledge base and give you a folder to upload PDFs
into. The Krova version is pointed at a specific problem: every time the agent
escalates, it names what it did not know. This is where that gets fixed.

Deliberately not a vector store. A small business's knowledge is small - a
price list, opening hours, a page of FAQs - and it fits in a prompt whole.
Chunking and embedding that would add a vector index, a retrieval step that
can miss, and tenant scoping on a shared index, which is the single highest
risk line of code in a multi-tenant product. Retrieval becomes worth it when
a client's documents outgrow the budget, and not before.

So a document is stored, its text extracted, and it goes to the agent intact.
The `token_estimate` column is what tells us when that stops being true.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class KnowledgeKind(str, enum.Enum):
    """
    What sort of thing this is.

    The kind matters because it changes how the agent should treat it. A price
    list is authoritative and must be quoted exactly; a policy constrains what
    the agent may say; an FAQ is a suggested answer it can rephrase.
    """

    price_list = "price_list"
    faq = "faq"
    policy = "policy"
    hours = "hours"
    service = "service"
    other = "other"


class KnowledgeSource(str, enum.Enum):
    upload = "upload"        # a file the owner gave us
    typed = "typed"          # written directly in Krova
    learned = "learned"      # extracted from conversations, awaiting confirmation


class KnowledgeItem(UUIDMixin, TimestampMixin, Base):
    """One thing the business knows."""

    __tablename__ = "knowledge_items"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[KnowledgeKind] = mapped_column(
        EnumType(KnowledgeKind, 20), nullable=False, default=KnowledgeKind.other
    )
    source: Mapped[KnowledgeSource] = mapped_column(
        EnumType(KnowledgeSource, 20), nullable=False, default=KnowledgeSource.typed
    )

    # The text the agent actually reads. For an upload this is the extracted
    # content, not the file - we never send a PDF to the model.
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Original filename and type, when it came from a file. Kept so an owner
    # recognises their own document in a list.
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Roughly what this costs to include, in tokens. The signal for when a
    # client has outgrown whole-document injection and needs retrieval.
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Off by default for learned items: something inferred from a conversation
    # is a suggestion until the owner confirms it, exactly like an unconfirmed
    # commitment.
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # The escalation this was added to answer, when an owner fixes a named gap.
    # Lets us tell them "the agent can answer this now".
    resolves_gap: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("idx_knowledge_business_active", "business_id", "active"),
    )
