"""Receivables import history.

Revision ID: f5a6b7c8d9e0
Revises: a7c8d9e0f1b2
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f5a6b7c8d9e0"
down_revision: str = "a7c8d9e0f1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "import_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "business_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("mark_missing_paid", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resolved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("import_runs")
