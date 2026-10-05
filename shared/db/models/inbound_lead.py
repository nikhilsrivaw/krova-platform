"""
A lead that arrived from an outside listing platform (Justdial first).

The raw payload is kept exactly as received. Justdial's real field names are
not confirmed from a live account yet, so the parser in shared/leads/justdial.py
works from assumptions. Storing the raw JSON means a mismatch is fixed by
reading one row, not by asking the business to send the lead again.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin


class InboundLead(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "inbound_leads"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    # The platform's own id for the lead, when it sends one. Used to skip a
    # lead that is delivered twice.
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    # received | duplicate | no_phone
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
    )
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_inbound_leads_business_received", "business_id", "received_at"),
        Index("idx_inbound_leads_external", "business_id", "source", "external_id"),
    )
