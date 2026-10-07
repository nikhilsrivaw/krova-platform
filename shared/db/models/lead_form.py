"""
A business's own hosted public form (name/phone/custom fields) for
collecting leads - a third way a lead reaches KROVA, alongside a listing
platform's webhook and manual/CSV entry. A submission funnels through the
exact same shared/leads/intake.py::ingest_parsed() every other lead source
uses, so it gets the same dedupe-by-platform-id (skipped here - a browser
submit is not a retry-prone webhook), customer resolution, and
lead.received automation every other source already gets.

Deliberately one business -> many forms (a clinic might want a "book a
callback" form and a separate "join our newsletter" form), each with its
own opaque public token (same convention as every other lead-source
webhook URL in this codebase - see shared/leads/intake.py's new_token())
rather than a human-chosen slug - avoids collision handling entirely, at
the cost of a link that looks like a generated id rather than a word. Same
tradeoff Google Forms makes for its own form links.

Reuses the same rate-limit columns (and shared/channels/web/guardrails.py's
check_rate_limit) the public web-chat widget already uses - a public,
unauthenticated POST endpoint is the same kind of exposure either way.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin


class LeadForm(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "lead_forms"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Opaque public identifier - see module docstring.
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # Ordered list of {key, label, type, required, options?}. type is one of
    # "name" | "phone" | "email" | "text" | "textarea" | "select" | "checkbox".
    # Validated in the router, not the database - this stays a plain JSONB
    # list so a business can reorder/add/remove fields freely.
    fields: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    rate_limit_window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rate_limit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("idx_lead_forms_business", "business_id"),
    )
