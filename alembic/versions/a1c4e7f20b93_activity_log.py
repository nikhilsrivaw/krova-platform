"""Activity log: who on the team did what.

Revision ID: a1c4e7f20b93
Revises: 88161202625e
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1c4e7f20b93"
down_revision: str = "88161202625e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "activity_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "business_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("user_label", sa.String(255), nullable=False, server_default=""),
        sa.Column("role", sa.String(20), nullable=True),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("summary", sa.String(300), nullable=False),
        sa.Column("outcome", sa.String(10), nullable=False, server_default="ok"),
        sa.Column("method", sa.String(8), nullable=False, server_default=""),
        sa.Column("path", sa.String(200), nullable=False, server_default=""),
        sa.Column("status_code", sa.Integer, nullable=True),
        sa.Column("detail", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(300), nullable=True),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("idx_activity_business_time", "activity_log", ["business_id", "occurred_at"])
    op.create_index(
        "idx_activity_business_user_time", "activity_log", ["business_id", "user_id", "occurred_at"]
    )
    op.create_index(
        "idx_activity_business_action_time", "activity_log", ["business_id", "action", "occurred_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_activity_business_action_time", table_name="activity_log")
    op.drop_index("idx_activity_business_user_time", table_name="activity_log")
    op.drop_index("idx_activity_business_time", table_name="activity_log")
    op.drop_table("activity_log")
