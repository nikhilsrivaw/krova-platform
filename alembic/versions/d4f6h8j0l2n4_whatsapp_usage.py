"""WhatsApp messages sent per month, by Meta's pricing category and type.

Revision ID: d4f6h8j0l2n4
Revises: c3e5g7i9k1m3
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4f6h8j0l2n4"
down_revision: str = "c3e5g7i9k1m3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("month", sa.DateTime(timezone=False), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("pricing_type", sa.String(40), nullable=False),
        sa.Column("messages", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("business_id", "month", "category", "pricing_type", name="uq_whatsapp_usage_month"),
    )


def downgrade() -> None:
    op.drop_table("whatsapp_usage")
