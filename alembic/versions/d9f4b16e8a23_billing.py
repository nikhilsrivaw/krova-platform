"""Billing: PayU payments, monthly subscriptions, the rupee wallet.

Revision ID: d9f4b16e8a23
Revises: c8e3a05d7f12
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d9f4b16e8a23"
down_revision: str = "c8e3a05d7f12"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "wallets",
        sa.Column("business_id", UUID, sa.ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("balance_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("calls_settled_through", sa.DateTime(timezone=True), nullable=True),
        sa.Column("low_balance_alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "wallet_entries",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("business_id", UUID, sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("balance_after_paise", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("ref", sa.String(80), nullable=True),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_wallet_entries_business", "wallet_entries", ["business_id", "created_at"])
    op.create_index(
        "uq_wallet_entry_ref", "wallet_entries", ["business_id", "kind", "ref"],
        unique=True, postgresql_where=sa.text("ref IS NOT NULL"),
    )
    op.create_table(
        "payments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("business_id", UUID, sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(12), nullable=False),
        sa.Column("plan", sa.String(20), nullable=True),
        sa.Column("base_paise", sa.Integer(), nullable=False),
        sa.Column("gst_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fee_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_paise", sa.Integer(), nullable=False),
        sa.Column("txnid", sa.String(25), nullable=False, unique=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("payu_id", sa.String(40), nullable=True),
        sa.Column("mode", sa.String(20), nullable=True),
        sa.Column("failure", sa.String(300), nullable=True),
        sa.Column("raw", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_payments_business", "payments", ["business_id", "created_at"])
    op.create_table(
        "subscriptions",
        sa.Column("business_id", UUID, sa.ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("plan", sa.String(20), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="active"),
        sa.Column("authpayuid", sa.String(40), nullable=True),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_charge_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pre_debit_sent_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure", sa.String(300), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("subscriptions")
    op.drop_index("idx_payments_business", table_name="payments")
    op.drop_table("payments")
    op.drop_index("uq_wallet_entry_ref", table_name="wallet_entries")
    op.drop_index("idx_wallet_entries_business", table_name="wallet_entries")
    op.drop_table("wallet_entries")
    op.drop_table("wallets")
