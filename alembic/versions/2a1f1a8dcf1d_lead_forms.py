"""Hosted public lead-intake forms.

Revision ID: 2a1f1a8dcf1d
Revises: f5a6b7c8d9e0
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2a1f1a8dcf1d"
down_revision: str = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lead_forms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "business_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.String(1000), nullable=True),
        sa.Column("token", sa.String(64), nullable=False, unique=True),
        sa.Column("fields", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rate_limit_window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rate_limit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("idx_lead_forms_business", "lead_forms", ["business_id"])


def downgrade() -> None:
    op.drop_index("idx_lead_forms_business", table_name="lead_forms")
    op.drop_table("lead_forms")
