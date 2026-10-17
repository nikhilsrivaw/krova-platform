"""Payment links from a business's own gateway (Razorpay first).

Revision ID: d6f8a0b2c4e6
Revises: c5e7a9b1d3f5
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d6f8a0b2c4e6"
down_revision: str = "c5e7a9b1d3f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_gateway_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("gateway", sa.String(30), nullable=False),
        sa.Column("key_id", sa.String(120), nullable=False),
        sa.Column("key_secret", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("business_id", name="uq_payment_gateway_per_business"),
    )
    op.create_table(
        "payment_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("commitment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("commitments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("gateway", sa.String(30), nullable=False),
        sa.Column("external_id", sa.String(80), nullable=False),
        sa.Column("short_url", sa.Text(), nullable=False),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="created"),
        sa.Column("amount_paid_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recorded_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("gateway", "external_id", name="uq_payment_link_external"),
    )
    op.create_index("idx_payment_links_commitment", "payment_links", ["commitment_id"])
    op.create_index("idx_payment_links_open", "payment_links", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_payment_links_open", table_name="payment_links")
    op.drop_index("idx_payment_links_commitment", table_name="payment_links")
    op.drop_table("payment_links")
    op.drop_table("payment_gateway_connections")
