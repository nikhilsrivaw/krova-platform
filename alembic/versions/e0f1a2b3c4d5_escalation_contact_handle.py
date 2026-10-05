"""Escalations keep the customer's Instagram id, so staff can find the thread.

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
"""

import sqlalchemy as sa
from alembic import op

revision: str = "e0f1a2b3c4d5"
down_revision: str = "d9e0f1a2b3c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("escalations", sa.Column("contact_handle", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("escalations", "contact_handle")
