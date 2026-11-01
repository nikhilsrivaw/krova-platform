"""Feedback: rounds (ask again until they answer, with gaps between) and customers added by hand - an uploaded list, a
pasted list, a form, or the API - for things that happened outside KROVA (a bus ticket sold on another app).

Revision ID: l2n4p6r8t0v2
Revises: k1m3o5q7s9u1
"""

import sqlalchemy as sa
from alembic import op

revision: str = "l2n4p6r8t0v2"
down_revision: str = "k1m3o5q7s9u1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("feedback_requests", sa.Column("round", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("feedback_requests", sa.Column("next_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("feedback_requests", sa.Column("source", sa.String(10), nullable=False, server_default="auto"))
    op.add_column("feedback_requests", sa.Column("happened_at", sa.DateTime(timezone=True), nullable=True))
    # Requests already waiting keep going under the new rules: their first round is due when they were.
    op.execute("UPDATE feedback_requests SET next_at = send_at WHERE status = 'due'")
    op.execute("UPDATE feedback_requests SET next_at = call_at, round = 1 WHERE status = 'sent' AND call_at IS NOT NULL")
    op.create_index("idx_feedback_requests_next", "feedback_requests", ["status", "next_at"])


def downgrade() -> None:
    op.drop_index("idx_feedback_requests_next", table_name="feedback_requests")
    for column in ("happened_at", "source", "next_at", "round"):
        op.drop_column("feedback_requests", column)
