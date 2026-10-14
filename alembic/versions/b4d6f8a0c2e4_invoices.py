"""GST invoices for plan payments and receipts for wallet top-ups.

Revision ID: b4d6f8a0c2e4
Revises: a3c9e5f7b1d2
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b4d6f8a0c2e4"
down_revision: str = "a3c9e5f7b1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invoice_counters",
        sa.Column("kind", sa.String(10), primary_key=True),
        sa.Column("fy", sa.String(4), primary_key=True),
        sa.Column("last", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("number", sa.String(20), nullable=False, unique=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("buyer_name", sa.String(200), nullable=False),
        sa.Column("buyer_address", sa.Text(), nullable=True),
        sa.Column("buyer_state_code", sa.String(2), nullable=False),
        sa.Column("buyer_gstin", sa.String(15), nullable=True),
        sa.Column("buyer_email", sa.String(320), nullable=True),
        sa.Column("service_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("service_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lines", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("taxable_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cgst_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sgst_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("igst_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fee_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_paise", sa.Integer(), nullable=False),
        sa.Column("payment_mode", sa.String(20), nullable=True),
        sa.Column("payment_ref", sa.String(40), nullable=True),
        sa.Column("next_renewal", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extra", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_invoices_business", "invoices", ["business_id", "issued_at"])


def downgrade() -> None:
    op.drop_index("idx_invoices_business", table_name="invoices")
    op.drop_table("invoices")
    op.drop_table("invoice_counters")
