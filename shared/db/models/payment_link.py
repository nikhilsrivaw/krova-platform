"""
Payment links from a business's own payment gateway (Razorpay first).

The money never touches KROVA: a link is created in the business's own gateway account with the key they gave us, the
customer pays on the gateway's page, and the money settles to the business's bank. KROVA only sends the link on
WhatsApp and checks back whether it was paid, so the matching Commitment can be marked paid by itself.

PaymentGatewayConnection holds the key (secret encrypted at rest, like every other secret). PaymentLink is one link,
tied to the Commitment it collects - a token's or an appointment's fee, a balance, an advance.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.models.channel import ConnectionStatus
from shared.db.types import EnumType


class PaymentGatewayConnection(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "payment_gateway_connections"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    # "razorpay" today; the adapter in shared/payments/gateways.py decides what each name means.
    gateway: Mapped[str] = mapped_column(String(30), nullable=False)
    key_id: Mapped[str] = mapped_column(String(120), nullable=False)
    # Encrypted at rest (shared/auth/encryption.py).
    key_secret: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ConnectionStatus] = mapped_column(
        EnumType(ConnectionStatus, 20), nullable=False, default=ConnectionStatus.active
    )
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("business_id", name="uq_payment_gateway_per_business"),
    )


class PaymentLink(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "payment_links"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    commitment_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("commitments.id", ondelete="CASCADE"), nullable=False
    )
    gateway: Mapped[str] = mapped_column(String(30), nullable=False)
    # The gateway's own id for the link (Razorpay: plink_...).
    external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    short_url: Mapped[str] = mapped_column(Text, nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    # The gateway's status words: created, partially_paid, paid, expired, cancelled.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="created")
    # What the gateway says has been paid, and how much of that is already written to the Commitment - so a check that
    # runs twice never records the same money twice.
    amount_paid_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recorded_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("gateway", "external_id", name="uq_payment_link_external"),
        Index("idx_payment_links_commitment", "commitment_id"),
        Index("idx_payment_links_open", "status", "created_at"),
    )
