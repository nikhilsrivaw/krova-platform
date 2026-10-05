"""Outbound campaigns say whether they are a service call or a promotional call.

Revision ID: f1a2b3c4d5e6
Revises: e0f1a2b3c4d5
"""

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str = "e0f1a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("call_campaigns", sa.Column("purpose", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("call_campaigns", "purpose")
