"""
One row per receivables import (a CSV/Excel upload, or a Zoho sync), so the
business can see what was imported, when, and what it changed.
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin


class ImportRun(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "import_runs"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mark_missing_paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resolved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
