"""commitment reminder_sent_at - generic recall-reminder support

Revision ID: a1b2c3d4e5f6
Revises: c1a4f2b6e08d
Create Date: 2026-09-05 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: str | None = 'c1a4f2b6e08d'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('commitments', sa.Column('reminder_sent_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('commitments', 'reminder_sent_at')
