"""Escalations carry the caller's request, a callback number, a status and a due time.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
"""

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str = "e5f6a7b8c9d0"
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
