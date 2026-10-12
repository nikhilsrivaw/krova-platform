"""Team calls: each member's own phone, and who handled / wrapped up a call.

Revision ID: f2b8d4e6a1c7
Revises: e1a7c93b5d04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2b8d4e6a1c7"
down_revision: str = "e1a7c93b5d04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # E.164 without the plus, like every phone number here. Where a call to this person rings.
    op.add_column("business_members", sa.Column("phone", sa.String(20), nullable=True))

    op.add_column(
        "calls",
        sa.Column(
            "handled_by_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    # Set when the person who spoke to the customer has written up the call.
    op.add_column("calls", sa.Column("wrapped_up_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("calls", sa.Column("wrapup_note", sa.Text(), nullable=True))
    op.create_index("idx_calls_handled_by", "calls", ["handled_by_user_id", "wrapped_up_at"])


def downgrade() -> None:
    op.drop_index("idx_calls_handled_by", table_name="calls")
    op.drop_column("calls", "wrapup_note")
    op.drop_column("calls", "wrapped_up_at")
    op.drop_column("calls", "handled_by_user_id")
    op.drop_column("business_members", "phone")
