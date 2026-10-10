"""Wallet: remember when a number's rent could not be taken.

Revision ID: e1a7c93b5d04
Revises: d9f4b16e8a23
"""

import sqlalchemy as sa
from alembic import op

revision: str = "e1a7c93b5d04"
down_revision: str = "d9f4b16e8a23"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wallets", sa.Column("rent_short_since", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("wallets", "rent_short_since")
