"""Voice roadmap round 4: real ring-time capture for the trust signal

Revision ID: d6e7f8g9h0i1
Revises: c5d6e7f8g9h0
Create Date: 2026-09-09 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'd6e7f8g9h0i1'
down_revision: str | None = 'c5d6e7f8g9h0'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('calls', sa.Column('ring_started_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('calls', 'ring_started_at')
