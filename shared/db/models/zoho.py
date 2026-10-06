"""
One Zoho Books connection per business.

The refresh token is stored encrypted (shared/auth/encryption.py), never plain.
It is what lets sync run without the business logging in again.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin


class ZohoConnection(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "zoho_connections"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
