"""
Money the business pays KROVA: monthly plans (auto-renewed through a PayU
mandate) and the rupee wallet that pays for voice (calls at cost + 25%, and the
rent on each phone number). Everything is integer paise.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, UUIDMixin


class Wallet(Base):
    """One per business. The balance only changes together with a WalletEntry."""

    __tablename__ = "wallets"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True
    )
    balance_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Voice usage created up to here has already been charged
    # (shared/billing/wallet.py::settle_voice_usage) - a watermark, so the
    # append-only usage_events table never has to be edited.
    calls_settled_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    low_balance_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WalletEntry(UUIDMixin, Base):
    """One credit (+) or debit (-). `ref` makes a charge idempotent: the same (kind, ref) cannot post twice."""

    __tablename__ = "wallet_entries"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    # topup | calls | number_rent | adjustment
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_wallet_entries_business", "business_id", "created_at"),
        Index(
            "uq_wallet_entry_ref", "business_id", "kind", "ref", unique=True,
            postgresql_where=text("ref IS NOT NULL"),
        ),
    )


class Payment(UUIDMixin, Base):
    """One attempt to collect money through PayU: a plan charge, a renewal, or a wallet top-up."""

    __tablename__ = "payments"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    # plan (first charge, registers the mandate) | renewal | topup
    purpose: Mapped[str] = mapped_column(String(12), nullable=False)
    plan: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # What the customer is charged = base + GST + gateway fee. For a top-up,
    # base_paise is the credit that lands in the wallet.
    base_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    gst_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fee_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    # The transaction id we send PayU (max 25 characters), unique platform-wide.
    txnid: Mapped[str] = mapped_column(String(25), nullable=False, unique=True)
    # pending | success | failed
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="pending")
    payu_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    failure: Mapped[str | None] = mapped_column(String(300), nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("idx_payments_business", "business_id", "created_at"),)


class Subscription(Base):
    """
    A business's monthly plan. `status`:
      active     paid up, will renew
      past_due   a renewal failed; we keep trying until `retry_until`
      suspended  retries ran out - AI sending and campaigns are off until paid
      cancelled  will not renew; works until `current_period_end`
    A business with NO row here (everyone who signed up before billing) is not restricted.
    """

    __tablename__ = "subscriptions"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True
    )
    plan: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="active")
    # The mihpayid of the registration payment - what PayU calls authpayuid
    # when we ask it to charge the mandate again.
    authpayuid: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # One month, GST included: the mandate's amount.
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # When the next charge is due. PayU must be told 48h before.
    next_charge_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pre_debit_sent_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure: Mapped[str | None] = mapped_column(String(300), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
