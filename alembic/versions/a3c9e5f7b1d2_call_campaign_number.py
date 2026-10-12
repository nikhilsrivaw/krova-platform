"""A voice campaign can name the number it calls from.

Revision ID: a3c9e5f7b1d2
Revises: f2b8d4e6a1c7
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a3c9e5f7b1d2"
down_revision: str = "f2b8d4e6a1c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Null = choose automatically (the number whose series suits the campaign's purpose).
    op.add_column(
        "call_campaigns",
        sa.Column(
            "connection_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channel_connections.id", ondelete="SET NULL"), nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("call_campaigns", "connection_id")
