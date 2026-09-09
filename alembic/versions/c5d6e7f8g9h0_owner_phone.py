"""Voice roadmap round 3: owner phone for the owner voice interface

Revision ID: c5d6e7f8g9h0
Revises: b4c5d6e7f8g9
Create Date: 2026-09-09 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'c5d6e7f8g9h0'
down_revision: str | None = 'b4c5d6e7f8g9'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('businesses', sa.Column('owner_phone', sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column('businesses', 'owner_phone')
