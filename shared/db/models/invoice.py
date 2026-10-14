"""
Invoices and receipts KROVA's seller, Aqirox Technology Private Limited, gives for payments.

`invoice`  a GST tax invoice, for a plan payment (first payment or monthly renewal).
`receipt`  a payment receipt for a wallet top-up. It is not a tax invoice: no GST on the wallet credit.

A document is a snapshot taken when the payment succeeds - the buyer's details, the amounts and the number
never change afterwards, even if the business edits its billing details later. One per payment.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, UUIDMixin


class InvoiceCounter(Base):
    """The last number used in one series (kind + financial year). Bumped atomically; never reused."""

    __tablename__ = "invoice_counters"

    kind: Mapped[str] = mapped_column(String(10), primary_key=True)
    fy: Mapped[str] = mapped_column(String(4), primary_key=True)
    last: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Invoice(UUIDMixin, Base):
    __tablename__ = "invoices"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # invoice | receipt
    number: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # The buyer as they were when it was issued.
    buyer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    buyer_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer_state_code: Mapped[str] = mapped_column(String(2), nullable=False)
    buyer_gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    buyer_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    service_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    service_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # [{"description", "detail", "amount_paise"}] - what is printed, line by line.
    lines: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    taxable_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cgst_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sgst_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    igst_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fee_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_paise: Mapped[int] = mapped_column(Integer, nullable=False)

    payment_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    payment_ref: Mapped[str | None] = mapped_column(String(40), nullable=True)
    next_renewal: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("idx_invoices_business", "business_id", "issued_at"),)
