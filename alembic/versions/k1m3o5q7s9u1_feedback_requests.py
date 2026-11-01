"""Feedback: asking a customer how a visit, token, order or chat went - on WhatsApp, by call, or both - and keeping the
answer.

Revision ID: k1m3o5q7s9u1
Revises: j0l2n4p6r8t0
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "k1m3o5q7s9u1"
down_revision: str = "j0l2n4p6r8t0"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "feedback_requests",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("business_id", UUID, sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", UUID, sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trigger", sa.String(20), nullable=False),
        sa.Column("ref", sa.String(64), nullable=False),
        sa.Column("about", sa.String(200), nullable=False),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="due"),
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("call_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("comment", sa.String(2000), nullable=True),
        sa.Column("answered_via", sa.String(10), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("awaiting_comment", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("review_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("business_id", "trigger", "ref", name="uq_feedback_request_ref"),
    )
    op.create_index("idx_feedback_requests_due", "feedback_requests", ["status", "send_at"])
    op.create_index("idx_feedback_requests_business", "feedback_requests", ["business_id", "created_at"])
    op.create_index("idx_feedback_requests_customer", "feedback_requests", ["customer_id"])


def downgrade() -> None:
    op.drop_index("idx_feedback_requests_customer", table_name="feedback_requests")
    op.drop_index("idx_feedback_requests_business", table_name="feedback_requests")
    op.drop_index("idx_feedback_requests_due", table_name="feedback_requests")
    op.drop_table("feedback_requests")
