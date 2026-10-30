"""Team chats: a chat can be marked resolved, and each person's last read of a chat is kept (for unread counts).

Revision ID: e5g7i9k1m3o5
Revises: d4f6h8j0l2n4
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5g7i9k1m3o5"
down_revision: str = "d4f6h8j0l2n4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "customers",
        sa.Column("resolved_by_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_table(
        "conversation_reads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "customer_id", name="uq_conversation_read"),
    )
    op.create_index("idx_conversation_reads_user", "conversation_reads", ["user_id", "business_id"])


def downgrade() -> None:
    op.drop_index("idx_conversation_reads_user", table_name="conversation_reads")
    op.drop_table("conversation_reads")
    op.drop_column("customers", "resolved_by_user_id")
    op.drop_column("customers", "resolved_at")
