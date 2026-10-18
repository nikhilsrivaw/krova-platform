"""Issues a business raises with KROVA's team from the Help page.

Revision ID: a1c3e5g7i9k1
Revises: f8b0d2e4a6c8
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1c3e5g7i9k1"
down_revision: str = "f8b0d2e4a6c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "support_tickets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("category", sa.String(20), nullable=False, server_default="other"),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("page", sa.String(100), nullable=True),
        sa.Column("transcript", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("reply", sa.Text(), nullable=True),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replied_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_support_tickets_business", "support_tickets", ["business_id", "created_at"])
    op.create_index("idx_support_tickets_status", "support_tickets", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_support_tickets_status", table_name="support_tickets")
    op.drop_index("idx_support_tickets_business", table_name="support_tickets")
    op.drop_table("support_tickets")
