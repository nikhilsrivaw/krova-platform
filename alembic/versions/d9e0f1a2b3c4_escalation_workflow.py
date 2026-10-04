"""Escalations carry the caller's request, a callback number, a status and a due time.

Revision ID: d9e0f1a2b3c4
Revises: c2d3e4f5a6b7
"""

import sqlalchemy as sa
from alembic import op

revision: str = "d9e0f1a2b3c4"
down_revision: str = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("escalations", sa.Column("caller_phone", sa.Text(), nullable=True))
    op.add_column("escalations", sa.Column("request_summary", sa.Text(), nullable=True))
    op.add_column(
        "escalations",
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
    )
    op.add_column("escalations", sa.Column("due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("escalations", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("escalations", sa.Column("resolution_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("escalations", "resolution_note")
    op.drop_column("escalations", "resolved_at")
    op.drop_column("escalations", "due_at")
    op.drop_column("escalations", "status")
    op.drop_column("escalations", "request_summary")
    op.drop_column("escalations", "caller_phone")
