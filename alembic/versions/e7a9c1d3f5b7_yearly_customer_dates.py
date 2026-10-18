"""Customer dates that come round every year (birthday, anniversary, yearly AMC).

Revision ID: e7a9c1d3f5b7
Revises: d6f8a0b2c4e6
"""

import sqlalchemy as sa
from alembic import op

revision: str = "e7a9c1d3f5b7"
down_revision: str = "d6f8a0b2c4e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customer_dates",
        sa.Column("repeats_yearly", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("customer_dates", "repeats_yearly")
