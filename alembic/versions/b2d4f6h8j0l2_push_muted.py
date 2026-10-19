"""Which kinds of phone notification each team member turned off.

Revision ID: b2d4f6h8j0l2
Revises: a1c3e5g7i9k1
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2d4f6h8j0l2"
down_revision: str = "a1c3e5g7i9k1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "business_members",
        sa.Column("push_muted", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("business_members", "push_muted")
