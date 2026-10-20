"""What was ticked from the fee list on a token or a booking.

Revision ID: c3e5g7i9k1m3
Revises: b2d4f6h8j0l2
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3e5g7i9k1m3"
down_revision: str = "b2d4f6h8j0l2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("queue_entries", sa.Column("fee_items", postgresql.JSONB(), nullable=True))
    op.add_column("appointments", sa.Column("fee_items", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("appointments", "fee_items")
    op.drop_column("queue_entries", "fee_items")
